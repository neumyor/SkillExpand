"""Locate where two executions of the same task first stop agreeing.

This is a pure function over execution records: no model, no environment, and no
knowledge of what the two runs were for.

An executor is a language model, so rewriting a Skill body can perturb a step
the rewritten rule does not govern: a change about answer format can alter the
first search query.  Two consequences follow, and this module exists for both.

* "The traces differ" is not the same claim as "the rule fired".  Only the
  verifier can attribute a difference to the rule; this module only finds it.
* An identical prefix is evidence of absence: when the two runs never diverge,
  the change did not affect anything, and a difference in outcome there is
  execution noise rather than an effect.

The comparison is over executed actions, not text: narration differs between any
two runs and would make every pair look divergent.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

#: Observations are context, not evidence of the outcome; keep them short.
OBSERVATION_CHARS = 400
#: How many identical actions before the divergence to show the verifier.
CONTEXT_STEPS = 2
#: Everything the verifier is shown.  Fixed so the audit can prove that no
#: field revealing how a run ended was added later.
PAYLOAD_KEYS = ('diverged_at_step', 'actions_before', 'context_observation',
                'action_without_change', 'action_with_change')


def action_sequence(events: Sequence[Dict[str, Any]]) -> Tuple[str, ...]:
    """The executed actions of one trial, in order.

    Only events that produced an action count.  A blocked or rejected action
    never ran, and treating it as a step would misalign the two sequences.
    """
    return tuple(str(event['action']) for event in events
                 if isinstance(event, dict) and event.get('action'))


def _last_observation_before(events: Sequence[Dict[str, Any]], step: int,
                             limit: int = OBSERVATION_CHARS) -> str:
    """The last observation the executor saw before the divergence.

    Taken from the shared prefix, so it is identical in both runs and carries no
    information about the difference -- which is why it is safe to show a
    verifier that must not see how either attempt ended.
    """
    seen = 0
    context = ''
    for event in events:
        if not isinstance(event, dict) or not event.get('action'):
            continue
        if seen >= step:
            break
        seen += 1
        if event.get('observation'):
            context = str(event['observation'])
    return context[:limit]


@dataclass(frozen=True)
class Divergence:
    """The first step at which two executions of one task stop agreeing."""

    step: int
    base_action: Optional[str]
    candidate_action: Optional[str]
    prefix_actions: Tuple[str, ...]
    context_observation: str

    def payload(self) -> Dict[str, Any]:
        # Trajectory lengths are deliberately absent: how long a run lasted is
        # a proxy for how it ended, which the verifier must not see.
        return {
            'diverged_at_step': self.step,
            'actions_before': list(self.prefix_actions),
            'context_observation': self.context_observation,
            'action_without_change': self.base_action,
            'action_with_change': self.candidate_action,
        }


def first_divergence(base_events: Sequence[Dict[str, Any]],
                     candidate_events: Sequence[Dict[str, Any]],
                     context_steps: int = CONTEXT_STEPS) -> Optional[Divergence]:
    """First differing step of two action sequences, or ``None`` if identical.

    A run that simply stops earlier is a divergence too: the shorter sequence
    has no action at that step, and the verifier should see that rather than a
    silent truncation.
    """
    base = action_sequence(base_events)
    candidate = action_sequence(candidate_events)
    shared = min(len(base), len(candidate))
    step = next((index for index in range(shared) if base[index] != candidate[index]),
                None)
    if step is None:
        if len(base) == len(candidate):
            return None
        step = shared
    return Divergence(
        step=step + 1,
        base_action=base[step] if step < len(base) else None,
        candidate_action=candidate[step] if step < len(candidate) else None,
        prefix_actions=base[max(0, step - context_steps):step],
        context_observation=_last_observation_before(base_events, step),
    )
