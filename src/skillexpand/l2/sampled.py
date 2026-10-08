"""The sampled-acceptance protocol: one structured edit, a claim, a paired
delta prediction, and a random val sample that corrects the prediction.

Everything specific to ``acceptance_mode='sampled'`` lives here, so the older
``predicted`` / ``empirical`` / ``jev`` paths keep their own code and their own
protocols.  This module is deliberately small: it fixes the names and the prompt
text the rest of the pipeline shares.  The estimator lives in
:mod:`skillexpand.evaluation.ppi`, the reviewer in
:mod:`skillexpand.evaluation.delta_review`, and the decision in
:mod:`skillexpand.evaluation.sampled_validation`.

Why the claim is part of this protocol rather than an option: a paired delta is
only checkable against something the proposer stated in advance.  Without a
claim the reviewer can only judge the edit as a whole, and the verifier has
nothing to confirm or refute.
"""

PROTOCOL = 'sampled-delta-acceptance-v1'

#: Appended to the Planner's system prompt when the claim is required.  It
#: restates the whole output schema for this protocol, because the claim travels
#: inside each hypothesis and a partial schema would be ambiguous.
CLAIM_CONTRACT = (
    ' Each hypothesis must also carry a falsifiable claim. Return JSON only as '
    '{"hypotheses":[{"mechanism":"...","change":"...",'
    '"claim":{"trigger":"the observable situation in which the new rule fires",'
    '"action_change":"the action the executor takes instead of the old behaviour"},'
    '"evidence":[{"card_id":"...","evidence_id":"..."}],'
    '"edit":{"op":"add|replace","section":"procedure|conditions|completion_checks",'
    '"target_id":"P1|C1|V1|null","text":"one concise rule"}}]}. '
    'trigger and action_change are single-line strings of at most 400 characters each. '
    'A verifier compares the claim with two execution traces of the same task, so state '
    'only what such traces can show: no rationale, no expected benefit, and no wording '
    'copied from the edit. Write trigger as a condition to look for in a trace, and '
    'action_change as the action the executor takes instead of its old behaviour.'
)


def claim_required(acceptance_mode: str) -> bool:
    """Whether this acceptance mode needs a claim with every proposal."""
    return acceptance_mode == 'sampled'


def validate_protocol(acceptance_mode: str, skill_edit_mode: str) -> None:
    """Reject combinations the protocol cannot express.

    A single added or replaced rule is what makes the delta attributable: the
    reviewer compares one specific before/after, and the claim names the trigger
    and the action of that one rule.  A whole-body rewrite has no such unit.
    """
    if acceptance_mode != 'sampled':
        return
    if skill_edit_mode != 'structured':
        raise ValueError(
            'sampled acceptance requires skill_edit_mode=structured: the paired '
            'delta and its claim are defined over one added or replaced rule')

#: Appended to the Planner's system prompt when its own history is supplied.
PLANNER_MEMORY_CONTRACT = (
    ' CHANGE HISTORY below records what your earlier proposals did when the val '
    'panel was actually executed, as counts and measurements. Use it to stop '
    'repeating a kind of change that measured nothing, and to state the trigger '
    'condition and the action change precisely enough to be checked against an '
    'execution trace. It deliberately names no task and no answer; do not infer '
    'one from it, and do not write rules for a panel you cannot see.'
)
