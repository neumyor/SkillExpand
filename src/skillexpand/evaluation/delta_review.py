"""The paired-delta Reviewer: one call per (task, proposal), judging the change.

The older predicted-val reviewer asked for two independent absolute success
probabilities -- one for the Skill as it stands and one for the candidate -- and
the pipeline then subtracted them.  Most of the variance in an absolute
probability belongs to the task's difficulty, not to the change, so the
difference of two such estimates is dominated by noise whenever the change is
small.  It also never showed the reviewer what had changed.

This reviewer is given the changed rule in before/after form, the Skill the
executor will actually see, and the Planner's claim, and returns a single paired
delta.  It reports the trigger probability separately because "this rule never
fires here" is the most common reason a well-intentioned change does nothing,
and stating it makes that judgement explicit rather than implicit in a number.

Nothing in this module touches an environment: it is a prediction, and the
sampled validator is what corrects it with real measurements.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from skillexpand import schema as S
from skillexpand import structured_skill as SS
from skillexpand.runtime import agent_factory as F
from skillexpand.evaluation.validation import ScoreCache
from skillexpand.reliability.errors import InvalidInput, JournalConflict, StageIncomplete
from skillexpand.reliability.policies import repair_policy
from skillexpand.reliability.retry import call_with_repair, fresh
from skillexpand.reliability.units import FailureCollector, map_units


class DeltaReviewError(StageIncomplete):
    """A panel whose reviewer calls include retryable task-local failures."""

    def __init__(self, errors: Sequence[Dict[str, Any]], expected_task_ids: Sequence[int]):
        errors = tuple(dict(error) for error in errors)
        details = ", ".join(
            f"{item['unit_id']}:{item['type']}: {item['message']}" for item in errors[:3]
        )
        omitted = len(errors) - 3
        if omitted > 0:
            details += f", ... ({omitted} more; see evaluation_errors)"
        super().__init__(
            f"Incomplete delta review ({len(errors)} failed task(s)): {details}", errors)
        self.errors = errors
        self.failed_task_ids = tuple(sorted(int(error["unit_id"]) for error in errors))
        self.expected_task_ids = tuple(sorted(int(t) for t in expected_task_ids))


def change_view(base_skill: S.Skill, candidate_skill: S.Skill) -> Dict[str, Any]:
    """The one rule this proposal changes, derived from the two bodies.

    The view is computed, not taken from the proposal's own ``edit`` field: the
    reviewer has to judge the change that is actually in the body, and a
    proposal whose recorded edit disagrees with its body is a protocol defect
    that the batch audit reports separately.
    """
    base = SS.from_legacy(base_skill.body)
    candidate = SS.from_legacy(candidate_skill.body)
    changes = []
    for section in SS.SECTION_NAMES:
        before = {row['id']: row['text'] for row in base[section]}
        after = {row['id']: row['text'] for row in candidate[section]}
        for rule_id in sorted(set(before) | set(after), key=_rule_sort_key):
            was, now = before.get(rule_id), after.get(rule_id)
            if was != now:
                changes.append({
                    'section': section, 'rule_id': rule_id,
                    'op': 'replace' if was is not None and now is not None else
                          ('add' if was is None else 'remove'),
                    'before': was, 'after': now})
    if len(changes) != 1:
        raise ValueError(
            f'paired-delta review requires exactly one changed rule, found {len(changes)}')
    return changes[0]


def _rule_sort_key(rule_id: str) -> Tuple[str, int]:
    return (rule_id[:1], int(rule_id[1:]) if rule_id[1:].isdigit() else 0)


@dataclass(frozen=True)
class DeltaPanelPrediction:
    """The Reviewer's per-task delta prediction over one whole panel."""

    base_skill_key: str
    candidate_skill_key: str
    claim_id: str
    task_ids: Tuple[int, ...]
    rows: Tuple[Dict[str, Any], ...]
    from_cache: int = 0
    measured: int = 0

    @property
    def deltas(self) -> Dict[int, float]:
        return {int(row['task_id']): float(row['delta_probability']) for row in self.rows}

    @property
    def mean_delta(self) -> Optional[float]:
        return (sum(self.deltas.values()) / len(self.rows)) if self.rows else None

    @property
    def mean_trigger(self) -> Optional[float]:
        if not self.rows:
            return None
        return sum(float(row['trigger_probability']) for row in self.rows) / len(self.rows)


class PairedDeltaReviewer:
    """Predicts the per-task effect of one rule change on a frozen val panel."""

    PROTOCOL = 'paired-delta-review-v1'
    REASON_MAX_CHARS = 8192
    RESPONSE_SCHEMA = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['trigger_probability', 'delta_probability', 'reason'],
        'properties': {
            'trigger_probability': {'type': 'number', 'minimum': 0, 'maximum': 1},
            'delta_probability': {'type': 'number', 'minimum': -1, 'maximum': 1},
            'reason': {'type': 'string', 'minLength': 1},
        },
    }
    OUTPUT_CONTRACT = (
        'Your visible final answer MUST be exactly one JSON object with only '
        'trigger_probability, delta_probability and reason. '
    )

    @classmethod
    def response_format(cls) -> Dict[str, Any]:
        return {
            'type': 'json_schema',
            'json_schema': {'name': 'paired_delta_review', 'strict': True,
                            'schema': cls.RESPONSE_SCHEMA},
        }

    def __init__(self, cfg, routes, cache, workers: int = 8,
                 host_factory: Optional[Callable] = None):
        self.cfg = cfg
        self.routes = routes
        self.cache = cache
        self.workers = max(1, int(workers))
        self.host_factory = host_factory
        self.protocol_hash = S.content_hash({
            'protocol': self.PROTOCOL,
            'response_schema': self.RESPONSE_SCHEMA,
            'benchmark': cfg.benchmark.name,
            'routes': routes.fingerprint,
        })

    def prompt(self, task: str, base_skill: S.Skill, candidate_skill: S.Skill,
               claim: S.Claim, memory_block: str = '') -> str:
        payload = {
            'task': task,
            'changed_rule': change_view(base_skill, candidate_skill),
            'skill_after': {'description': candidate_skill.description,
                            'body': SS.render(SS.from_legacy(candidate_skill.body))},
            'claim': claim.payload(),
            'instructions': (
                'You are a strict validation reviewer. A fresh executor will attempt '
                'this task exactly ONCE, with no retry, no reflection and no access to '
                'the answer, using the Skill below. Compare it with the same executor '
                'on this same task using the Skill before this one rule changed, and '
                'nothing else. '
                'Report trigger_probability: the probability that the changed rule '
                'changes what the executor does on THIS task. If the rule never fires '
                'here, delta_probability must be 0: a rule that does not fire cannot '
                'affect the outcome. '
                'Report delta_probability = P(success after the change) - P(success '
                'before), in [-1, 1]. Judge only what the change causes; the absolute '
                'difficulty of the task cancels out and must not appear in either '
                'field. Do not use any execution trace, and do not assume a rejected '
                'answer can be retried. You may reason internally for as long as '
                'needed. '
                f'{self.OUTPUT_CONTRACT}'
                'Do not output markdown, analysis, or any other key. reason is one '
                f'concise string of at most {self.REASON_MAX_CHARS} characters that '
                'names the situation the rule fires in, or why it cannot fire.'
            ),
            'output_schema': {
                'trigger_probability': 'number in [0,1]',
                'delta_probability': 'number in [-1,1]',
                'reason': f'string, <= {self.REASON_MAX_CHARS} characters',
            },
        }
        if memory_block:
            payload['reviewer_memory'] = memory_block
        return S._canonical_json(payload)

    def _parse_response(self, raw):
        from skillexpand.runtime.json_output import extract_json
        value = extract_json(raw, required_keys=('trigger_probability', 'delta_probability',
                                                 'reason'))
        if set(value) != {'trigger_probability', 'delta_probability', 'reason'}:
            raise ValueError('delta reviewer must return exactly three required fields')
        trigger = value['trigger_probability']
        delta = value['delta_probability']
        for name, number, low, high in (('trigger_probability', trigger, 0.0, 1.0),
                                        ('delta_probability', delta, -1.0, 1.0)):
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise ValueError(f'{name} must be numeric')
            if not low <= float(number) <= high:
                raise ValueError(f'{name} must lie in [{low}, {high}]')
        reason = value['reason']
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('delta reviewer reason must be a non-empty string')
        trigger, delta = float(trigger), float(delta)
        return {
            'trigger_probability': trigger,
            'delta_probability': delta,
            'reason': reason,
            # Recorded, not enforced: a reviewer that predicts a change while also
            # saying the rule never fires is contradicting itself, and that is a
            # finding for the analysis rather than a defect worth retrying.
            'claim_inconsistent': trigger <= 0.0 and abs(delta) > 0.0,
            'raw': value,
        }

    def _call(self, host, prompt):
        from langchain.schema import HumanMessage
        messages = [HumanMessage(content=prompt)]
        request_kwargs = {'response_format': self.response_format(),
                          'enable_thinking': True}
        return host.llm(messages, stop=[], replace_newline=False,
                        request_kwargs=request_kwargs)

    def _review(self, host, prompt):
        result = call_with_repair(repair_policy('reviewer.delta_review'),
                                  fresh(lambda: self._call(host, prompt)),
                                  self._parse_response)
        return result.value, result.attempts

    def predict(self, base_skill: S.Skill, candidate_skill: S.Skill, claim: S.Claim,
                task_ids: Sequence[int], panel_key: str,
                memory_blocks: Optional[Dict[int, str]] = None
                ) -> DeltaPanelPrediction:
        if base_skill.skill_id != candidate_skill.skill_id:
            raise InvalidInput('Both arms of a paired prediction must be the same Skill')
        task_ids = tuple(sorted(int(t) for t in task_ids))
        if not task_ids or not set(task_ids) <= set(self.routes.groups[base_skill.skill_id]):
            raise JournalConflict('Predicted tasks must belong to the frozen Skill route group')
        # The cache identity carries both bodies, the claim and the task's memory
        # block: the same request is never re-asked, and a request with different
        # memory is never served a stale row.  The memory is retrieved per task,
        # so it belongs here rather than in the protocol hash.
        blocks = dict(memory_blocks or {})
        material = S._canonical_json({'base': base_skill.body, 'candidate': candidate_skill.body,
                                      'claim': claim.payload()})
        keys = {
            t: ScoreCache.make_key(self.cfg.benchmark.name, panel_key, t,
                                   f'delta:{self.protocol_hash}',
                                   material + blocks.get(t, ''))
            for t in task_ids
        }
        records, pending = {}, []
        for task_id in task_ids:
            hit = self.cache.get(keys[task_id])
            if hit is None:
                pending.append(task_id)
            else:
                records[task_id] = hit

        def one(task_id):
            if self.host_factory is None:
                raise InvalidInput('Delta reviewer requires a host factory')
            host = self.host_factory(
                task_id,
                self.cache.path.parent / 'usage' /
                f'delta-{base_skill.skill_id}-{task_id}-{S.content_hash(material)}.json',
            )
            result, attempts = self._review(
                host, self.prompt(F.task_text_of(self.cfg, task_id), base_skill,
                                  candidate_skill, claim, blocks.get(task_id, '')))
            return {'task_id': task_id, 'base_skill_key': base_skill.key,
                    'candidate_skill_key': candidate_skill.key, 'claim_id': claim.claim_id,
                    'cache_key': keys[task_id], 'panel_key': panel_key,
                    'protocol_hash': self.protocol_hash,
                    'memory_hash': S.content_hash(blocks.get(task_id, '')),
                    'format_attempts': attempts,
                    'response_format': 'json_schema', **result}

        def store(task_id, record):
            self.cache.put(keys[task_id], record)
            records[task_id] = record

        collector = FailureCollector('delta-review',
                                     self.cache.path.parent / 'evaluation_errors')
        map_units(pending, one, workers=self.workers, collector=collector,
                  on_success=store, name=lambda task_id: keys[task_id],
                  thread_name_prefix='delta-review')
        if collector.failures:
            raise DeltaReviewError(collector.failures, task_ids)
        if set(records) != set(task_ids):
            raise JournalConflict('Delta review returned no record for some tasks')
        return DeltaPanelPrediction(
            base_skill.key, candidate_skill.key, claim.claim_id, task_ids,
            tuple(records[t] for t in task_ids),
            from_cache=len(task_ids) - len(pending), measured=len(pending),
        )
