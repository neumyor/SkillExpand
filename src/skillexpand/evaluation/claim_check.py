"""The independent verifier: did the changed rule change the execution, and how?

A claim is only worth stating if someone checks it, and the two parties to the
proposal are the wrong ones to do so.  The Planner wrote the rule; the Reviewer
predicted what it would do.  Letting either of them judge whether the prediction
held would let their own error stand.

So the verifier is a third role with no stake in the answer.  It reads both
executions of one task -- without the changed rule and with it -- and decides
whether they differ in behaviour, where they first do, and whether that
difference is the one the claim describes.  Comparing two trajectories is left
to the verifier on purpose: deciding by program whether two free-text searches
or two household action sequences "differ" needs benchmark-specific
normalisation and an open-ended list of corner cases.

It never sees how either attempt ended: the observation of each run's last step
-- where the environment reports the result -- is withheld, and no success flag
is shown.  Whether the change helped is the sampled measurement's question.
"""

from typing import Any, Callable, Dict, List, Optional, Sequence

from skillexpand import schema as S
from skillexpand.runtime import agent_factory as F
from skillexpand.evaluation.validation import ScoreCache
from skillexpand.reliability.errors import InvalidInput
from skillexpand.reliability.policies import repair_policy
from skillexpand.reliability.retry import call_with_repair, fresh

#: The verdicts, in the order the instruction lists them.
CATEGORIES = ('no_difference', 'claim_confirmed', 'claim_not_confirmed', 'unrelated',
              'indeterminate')



def trajectory_view(events: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    """One execution as the verifier reads it: what the executor wrote and saw.

    The last observation is dropped because it is where the environment reports
    the outcome (e.g. "Answer is CORRECT").  Nothing else is shortened or
    interpreted: a trigger condition often lives in an observation (a document
    returned by a search, the commands currently admissible), and an unmarked
    cut would let the verifier judge a partial page as if it were complete.
    """
    steps = [{'executor': str(event.get('model_text', '')),
              'observation': str(event.get('observation', ''))}
             for event in events if isinstance(event, dict)]
    if steps:
        steps[-1]['observation'] = ''
    return steps


class TrajectoryVerifier:
    """Frozen, third-party attribution of a trajectory difference to a rule."""

    PROTOCOL = 'claim-verification-v3'
    RESPONSE_SCHEMA = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['category', 'first_difference_step', 'reason'],
        'properties': {
            'category': {'type': 'string', 'enum': list(CATEGORIES)},
            'first_difference_step': {'type': ['integer', 'null'], 'minimum': 1},
            'reason': {'type': 'string', 'minLength': 1},
        },
    }

    @classmethod
    def response_format(cls) -> Dict[str, Any]:
        return {
            'type': 'json_schema',
            'json_schema': {'name': 'claim_verification', 'strict': True,
                            'schema': cls.RESPONSE_SCHEMA},
        }

    def __init__(self, cfg, cache, workers: int = 8,
                 host_factory: Optional[Callable] = None):
        self.cfg = cfg
        self.cache = cache
        self.workers = max(1, int(workers))
        self.host_factory = host_factory
        self.protocol_hash = S.content_hash({
            'protocol': self.PROTOCOL,
            'response_schema': self.RESPONSE_SCHEMA,
            'benchmark': cfg.benchmark.name,
        })

    def prompt(self, task: str, changed_rule: Dict[str, Any], claim: S.Claim,
               without_change: List[Dict[str, str]],
               with_change: List[Dict[str, str]]) -> str:
        return S._canonical_json({
            'task': task,
            'changed_rule': dict(changed_rule),
            'claim': claim.payload(),
            'execution_without_change': without_change,
            'execution_with_change': with_change,
            'instructions': (
                'You are an independent verifier. The same executor attempted the same '
                'task twice with the same Skill, except for the one rule in changed_rule: '
                'execution_without_change used the rule as it was, execution_with_change '
                'used it as changed. Each execution lists, step by step, what the executor '
                'wrote and what it observed. Decide whether the two executions differ in '
                'behaviour, and classify:\n'
                '- no_difference: they take the same course of action; wording that does '
                'not change what is done is not a difference.\n'
                '- claim_confirmed: they differ, and with the change the executor does what '
                "the claim's action_change describes, in the situation its trigger "
                'describes.\n'
                '- claim_not_confirmed: they differ because of the changed rule, but not in '
                'the way the claim describes, or not in the situation it names.\n'
                '- unrelated: they differ, but not because of this rule. The executor is a '
                'language model, so changing one rule can perturb behaviour the rule does '
                'not govern.\n'
                '- indeterminate: the executions do not show enough to decide.\n'
                'first_difference_step is the 1-based step of execution_with_change at '
                'which behaviour first differs, or null for no_difference or when it '
                'cannot be located. How either attempt ended is deliberately withheld; '
                'judge only whether and how this rule changed what the executor did, never '
                'whether the change helped. '
                f'Return JSON only: {{"category":"<one of {", ".join(CATEGORIES)}>",'
                '"first_difference_step":<integer or null>,'
                '"reason":"one or two sentences naming the two behaviours that differ, '
                'or why they do not"}.'
            ),
        })

    def _parse_response(self, raw):
        from skillexpand.runtime.json_output import extract_json
        keys = ('category', 'first_difference_step', 'reason')
        value = extract_json(raw, required_keys=keys)
        if set(value) != set(keys):
            raise ValueError('verifier must return exactly category, '
                             'first_difference_step and reason')
        category, step, reason = (value[key] for key in keys)
        if category not in CATEGORIES:
            raise ValueError(f'verifier category {category!r} is not one of {CATEGORIES}')
        if step is not None and (isinstance(step, bool) or not isinstance(step, int)
                                 or step < 1):
            raise ValueError('first_difference_step must be a positive integer or null')
        if category == 'no_difference' and step is not None:
            raise ValueError('no_difference cannot name a first difference step')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('verifier reason must be a non-empty string')
        return {'category': category, 'first_difference_step': step,
                'reason': reason.strip()}

    def _call(self, host, prompt):
        from langchain.schema import HumanMessage
        return host.llm(
            [HumanMessage(content=prompt)], stop=[], replace_newline=False,
            request_kwargs={'response_format': self.response_format(),
                            'enable_thinking': True})

    def verify(self, task_id: int, changed_rule: Dict[str, Any], claim: S.Claim,
               base_events: Sequence[Dict[str, Any]],
               candidate_events: Sequence[Dict[str, Any]], panel_key: str) -> Dict[str, Any]:
        """One verdict for one sampled task, from its two executions."""
        without_change = trajectory_view(base_events)
        with_change = trajectory_view(candidate_events)
        material = S._canonical_json({'rule': dict(changed_rule), 'claim': claim.payload(),
                                      'without': without_change, 'with': with_change})
        key = ScoreCache.make_key(self.cfg.benchmark.name, panel_key, task_id,
                                  f'verify:{self.protocol_hash}', material)
        hit = self.cache.get(key)
        if hit is not None:
            return dict(hit)
        if self.host_factory is None:
            raise InvalidInput('Claim verification requires a host factory')
        host = self.host_factory(
            task_id,
            self.cache.path.parent / 'usage' /
            f'verify-{task_id}-{S.content_hash(material)}.json')
        result = call_with_repair(
            repair_policy('verifier.claim'),
            fresh(lambda: self._call(
                host, self.prompt(F.task_text_of(self.cfg, task_id), changed_rule,
                                  claim, without_change, with_change))),
            self._parse_response)
        record = {'task_id': task_id, 'cache_key': key, 'panel_key': panel_key,
                  'protocol_hash': self.protocol_hash,
                  'format_attempts': result.attempts, **result.value}
        self.cache.put(key, record)
        return dict(record)
