"""The independent verifier: does the rule explain the execution difference?

A claim is only worth stating if someone checks it, and the two parties to the
proposal are the wrong ones to do so.  The Planner wrote the rule; the Reviewer
predicted what it would do.  Letting either of them judge whether the prediction
held would let their own error stand, and the Reviewer's verdict is one of the
things this evidence is used to score.

So the verifier is a third role with a deliberately narrow question and no stake
in the answer: given the changed rule, the claim, and the first step at which two
executions stop agreeing, decide whether the rule explains that difference.

It never sees the outcome of either attempt.  Everything it is shown -- the task,
the two actions at the divergence, and the observation *before* the divergence --
is identical in both runs, so the judgement cannot be read off which run ended
better.  Whether the change helped is the sampled measurement's question, not
this one.
"""

from typing import Any, Callable, Dict, Optional

from skillexpand import schema as S
from skillexpand.runtime import agent_factory as F
from skillexpand.evaluation.validation import ScoreCache
from skillexpand.reliability.errors import InvalidInput
from skillexpand.reliability.policies import repair_policy
from skillexpand.reliability.retry import call_with_repair, fresh

#: The four verdicts, in the order the instruction lists them.
CATEGORIES = ('claim_confirmed', 'claim_not_confirmed', 'unrelated', 'indeterminate')

REASON_MAX_CHARS = 2000


class TrajectoryVerifier:
    """Frozen, third-party attribution of a trajectory difference to a rule."""

    PROTOCOL = 'claim-verification-v1'
    RESPONSE_SCHEMA = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['category', 'reason'],
        'properties': {
            'category': {'type': 'string', 'enum': list(CATEGORIES)},
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
               execution_difference: Dict[str, Any]) -> str:
        return S._canonical_json({
            'task': task,
            'changed_rule': dict(changed_rule),
            'claim': claim.payload(),
            'execution_difference': dict(execution_difference),
            'instructions': (
                'You are an independent verifier. Two executors attempted the same '
                'task with the same Skill, except that one rule differs: the rule in '
                'changed_rule. Their executed actions first differ at the step shown '
                'in execution_difference, which also lists the actions they shared '
                'before that step and the observation the executor had seen just '
                'before it. Classify that difference:\n'
                '- claim_confirmed: the action taken WITH the change is the action the '
                "claim's action_change describes, in the situation its trigger "
                'describes.\n'
                '- claim_not_confirmed: the change is implicated in the difference, '
                'but the action is not what the claim describes, or the situation is '
                'not the one the claim names.\n'
                '- unrelated: the difference cannot be attributed to this rule at all. '
                'The executor is a language model, so rewriting one rule can perturb '
                'a step that rule does not govern; such a difference is unrelated, '
                'not confirmed.\n'
                '- indeterminate: the supplied evidence is not enough to decide.\n'
                'How either attempt ended is deliberately withheld, and no observation '
                'produced by the differing action is shown. Judge only whether this '
                'rule explains the difference, never whether the change helped, and '
                'never guess at success. '
                f'Return JSON only: {{"category":"<one of {", ".join(CATEGORIES)}>",'
                f'"reason":"at most {REASON_MAX_CHARS} characters, naming the '
                'evidenced situation and the two actions"}.'
            ),
            'output_schema': {'category': f'one of {", ".join(CATEGORIES)}',
                              'reason': f'string, <= {REASON_MAX_CHARS} characters'},
        })

    def _parse_response(self, raw):
        from skillexpand.runtime.json_output import extract_json
        value = extract_json(raw, required_keys=('category', 'reason'))
        if set(value) != {'category', 'reason'}:
            raise ValueError('verifier must return exactly category and reason')
        if value['category'] not in CATEGORIES:
            raise ValueError(f"verifier category {value['category']!r} is not one of "
                             f"{CATEGORIES}")
        reason = value['reason']
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('verifier reason must be a non-empty string')
        if len(reason) > REASON_MAX_CHARS:
            raise ValueError(f'verifier reason exceeds {REASON_MAX_CHARS} characters')
        return {'category': value['category'], 'reason': reason.strip()}

    def _call(self, host, prompt):
        from langchain.schema import HumanMessage
        return host.llm(
            [HumanMessage(content=prompt)], stop=[], replace_newline=False,
            request_kwargs={'response_format': self.response_format(),
                            'enable_thinking': True})

    def verify(self, task_id: int, changed_rule: Dict[str, Any], claim: S.Claim,
               divergence: Any, panel_key: str) -> Dict[str, Any]:
        """One verdict for one divergence; called only when one exists."""
        payload = divergence.payload()
        material = S._canonical_json({'rule': dict(changed_rule), 'claim': claim.payload(),
                                      'difference': payload})
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
                                  claim, payload))),
            self._parse_response)
        record = {'task_id': task_id, 'cache_key': key, 'panel_key': panel_key,
                  'protocol_hash': self.protocol_hash,
                  'format_attempts': result.attempts, **result.value}
        self.cache.put(key, record)
        return dict(record)
