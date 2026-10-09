"""The sampled-acceptance protocol: one structured edit, a claim, a paired
delta prediction, and a random val sample that corrects the prediction.

Everything specific to ``acceptance_mode='sampled'`` at the L2 layer lives here
or in the modules it names, so the older ``predicted`` / ``empirical``
paths keep their own code and their own protocols:

* the estimator: :mod:`skillexpand.evaluation.ppi`;
* the Reviewer: :mod:`skillexpand.evaluation.delta_review`;
* the verifier: :mod:`skillexpand.evaluation.claim_check`;
* the decision: :mod:`skillexpand.evaluation.sampled_validation`;
* the ledger and the two memories: :mod:`skillexpand.l2.ledger`,
  :mod:`skillexpand.l2.memory`.

The rest of the pipeline reaches this protocol only through the functions
below, so each older module carries a single dispatch point and no sampled
logic of its own.

Why the claim is part of this protocol rather than an option: a paired delta is
only checkable against something the proposer stated in advance.  Without a
claim the reviewer can only judge the edit as a whole, and the verifier has
nothing to confirm or refute.
"""

from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from skillexpand import schema as S
from skillexpand.evaluation.sampled_validation import MIN_SAMPLE_SIZE
from skillexpand.l2 import ledger as LED
from skillexpand.l2 import memory as MEM
from skillexpand.reliability.errors import InvalidInput

MODE = 'sampled'

#: The claim object inside each hypothesis of the Planner's output schema.
CLAIM_FIELD = ('"claim":{"trigger":"the observable situation in which the new rule fires",'
               '"action_change":"the action the executor takes instead of the old behaviour"},')

#: Appended to the Planner's system prompt when the claim is required.
CLAIM_CONTRACT = (
    ' Every hypothesis must carry the claim object shown in the schema. trigger and '
    'action_change are single-line strings of at most 400 characters each. A verifier '
    'compares the claim with two execution traces of the same task, so state only what '
    'such traces can show: no rationale, no expected benefit, and no wording copied from '
    'the edit. Write trigger as a condition to look for in a trace, and action_change as '
    'the action the executor takes instead of its old behaviour.'
)

#: Appended to the Planner's system prompt when its own history is supplied.
PLANNER_MEMORY_CONTRACT = (
    ' change_history records what your earlier proposals did when the val panel was '
    'actually executed, as counts and measurements. Use it to stop repeating a kind of '
    'change that measured nothing, and to state the trigger condition and the action '
    'change precisely enough to be checked against an execution trace. It deliberately '
    'names no task and no answer; do not infer one from it, and do not write rules for a '
    'panel you cannot see.'
)

PROMPTS = ('CLAIM_FIELD', 'CLAIM_CONTRACT', 'PLANNER_MEMORY_CONTRACT')


def claim_required(acceptance_mode: str) -> bool:
    """Whether this acceptance mode needs a claim with every proposal."""
    return acceptance_mode == MODE


#: The protocol's own switches and their defaults, shared by the CLI, the
#: campaign manifest and ``EvolutionConfig`` so the three cannot drift apart.
DEFAULTS = {
    'acceptance_sample_size': 16,
    'acceptance_confidence': 0.9,
    'claim_verification': 'on',
    'planner_memory_mode': 'aggregate',
    'reviewer_memory_mode': 'cases',
}
CHOICES = {
    'claim_verification': ('on', 'off'),
    'planner_memory_mode': ('aggregate', 'off'),
    'reviewer_memory_mode': ('cases', 'off'),
}


_HELP = {
    'acceptance_sample_size': 'Ceiling on executed val tasks per candidate (sampled only)',
    'acceptance_confidence': 'One-sided confidence level of the sampled lower bound',
    'claim_verification': 'Independent verifier comparing the two executions against '
                          'the claim (sampled only)',
    'planner_memory_mode': "Planner's aggregate history of its own proposals (sampled only)",
    'reviewer_memory_mode': "Reviewer's cases of its own misestimates (sampled only)",
}


def add_arguments(parser) -> None:
    """Register the protocol switches on an argparse parser (CLI and campaign)."""
    for key, default in DEFAULTS.items():
        flag = '--' + key.replace('_', '-')
        if key in CHOICES:
            parser.add_argument(flag, choices=CHOICES[key], default=default,
                                help=_HELP[key])
        else:
            parser.add_argument(flag, type=type(default), default=default, help=_HELP[key])


def options_from(args) -> Dict[str, Any]:
    return {key: getattr(args, key) for key in DEFAULTS}


def validate_options(options: Dict[str, Any]) -> None:
    """Reject switch values and combinations the protocol cannot express.

    Applies to every protocol, because the switches are frozen into the
    manifest either way.  The sampled protocol additionally requires the
    structured edit mode: a single added or replaced rule is what makes the
    delta attributable.
    """
    if int(options['acceptance_sample_size']) < MIN_SAMPLE_SIZE:
        raise InvalidInput(f'acceptance_sample_size must be at least {MIN_SAMPLE_SIZE}')
    if not 0.0 < float(options['acceptance_confidence']) < 1.0:
        raise InvalidInput('acceptance_confidence must lie strictly between 0 and 1')
    for key, allowed in CHOICES.items():
        if options[key] not in allowed:
            raise InvalidInput(f'{key} must be one of {allowed}')
    if options['acceptance_mode'] != MODE:
        return
    if options['skill_edit_mode'] != 'structured':
        raise InvalidInput(
            'sampled acceptance requires skill_edit_mode=structured: the paired '
            'delta and its claim are defined over one added or replaced rule')


def parse_claim(raw: Any) -> Dict[str, str]:
    """Validate the claim a hypothesis carries and bind it to a program id.

    The id is assigned here, never accepted from the model: an id the model
    could choose would not bind the text it accompanies.
    """
    if not isinstance(raw, dict) or set(raw) != {'trigger', 'action_change'}:
        raise ValueError('claim must contain exactly trigger and action_change')
    try:
        return S.Claim(raw['trigger'], raw['action_change']).to_dict()
    except (ValueError, TypeError) as exc:
        raise ValueError(f'Invalid claim: {exc}') from exc


def claim_of(hypothesis: Dict[str, Any]) -> S.Claim:
    return S.Claim(hypothesis['claim']['trigger'], hypothesis['claim']['action_change'])


def accept(validator, base_skill: S.Skill, ordered: Sequence[S.CandidateSkill],
           aliases: Dict[str, S.CandidateSkill], claims: Dict[str, S.Claim],
           acceptance_record: Optional[Dict[str, Any]] = None
           ) -> Tuple[Dict[str, Any], Optional[str]]:
    """Validate every proposed candidate and apply the fixed decision rule.

    Returns the journal fields and the alias of the installed candidate, if any.
    Every candidate is sampled whether or not it is later accepted, so the
    measurements are never conditioned on the decision.  With a recorded
    ``acceptance_record`` (offline replay) nothing is executed or predicted.
    """
    alias_of = {candidate.candidate_id: alias for alias, candidate in aliases.items()}
    if acceptance_record is not None:
        acceptance = dict(acceptance_record)
    else:
        panel_key = f'val:{validator.routes.fingerprint}:{base_skill.skill_id}'
        panel = sorted(int(t) for t in validator.routes.groups[base_skill.skill_id])
        results = []
        # A family the selector routed no val task to cannot be measured; it
        # holds, as under the older protocols, instead of halting the run.
        for candidate in (ordered if panel else ()):
            claim = claims[candidate.candidate_id]
            validation = validator.validate(
                base_skill, candidate.skill, claim, panel_key,
                sample_key=f'{MODE}:{panel_key}:{candidate.candidate_id}',
                exclude_candidate_id=candidate.candidate_id)
            results.append({'id': alias_of[candidate.candidate_id],
                            'candidate_id': candidate.candidate_id,
                            'claim_id': claim.claim_id,
                            'executions': validation.executions,
                            'reviewer_requests': validation.reviewer_requests,
                            'result': validation.to_dict()})
        acceptance = {
            'mode': MODE,
            'scope': 'val',
            'panel': panel_key,
            'protocol_hash': validator.protocol_hash,
            'sample_size': validator.sample_size,
            'confidence': validator.confidence,
            'task_ids': panel,
            'candidate_ids': [candidate.candidate_id for candidate in ordered],
            'candidates': results,
            'executions': sum(row['executions'] for row in results),
            'predicted_requests': sum(row['reviewer_requests'] for row in results),
        }
    results = acceptance['candidates']
    approved = [row for row in results if row['result']['decision']['accepted']]
    # Ties between accepted candidates break on the stronger lower bound; with
    # the default single candidate the rank never matters.
    winner = max(approved, key=lambda row: (row['result']['decision']['lower'],
                                            row['result']['decision']['point'],
                                            row['id'])) if approved else None
    selected = winner['id'] if winner else None
    if winner:
        reason = 'sampled_approved: corrected delta clears zero'
    elif not acceptance['task_ids']:
        reason = 'hold: frozen val panel is empty'
    else:
        reason = 'hold: ' + results[0]['result']['decision']['reason']
    return {
        'selection_method': 'sampled_paired_delta',
        'acceptance': acceptance,
        'reason': reason,
        'selected_candidate_id': aliases[selected].candidate_id if selected else None,
        'outcome': 'review_approved' if selected else 'hold',
        'empirically_validated': False,
    }, selected


def round_memories(root, round_index: int, config,
                   task_text: Callable[[int], str]) -> Tuple[str, Optional[MEM.ReviewerMemory]]:
    """Both players' memories for one round, from rounds that finished before it.

    The loop injects exactly this and the audit recomputes exactly this, so the
    two cannot disagree about what a player was shown.
    """
    changes = LED.read_changes(root, before_round=round_index)
    planner = (MEM.PlannerMemory(changes, claims_verified=config.claim_verification == 'on')
               .render() if config.planner_memory_mode == 'aggregate' else '')
    reviewer = (MEM.ReviewerMemory.build(changes, task_text)
                if config.reviewer_memory_mode == 'cases' and changes else None)
    return planner, reviewer


def journal_fields(planner_memory: str,
                   reviewer_memory: Optional[MEM.ReviewerMemory]) -> Dict[str, Any]:
    """What a batch journal records about the memories its players were shown."""
    return {'planner_memory': planner_memory,
            'reviewer_memory_version': reviewer_memory.version if reviewer_memory else 0}


def summary_fields(config, records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Round-summary fields of this protocol, including the Reviewer metrics."""
    changes = [change for record in records for change in LED.changes_from_journal(record)]
    return {
        'acceptance_sample_size': config.acceptance_sample_size,
        'acceptance_confidence': config.acceptance_confidence,
        'claim_verification': config.claim_verification,
        'planner_memory_mode': config.planner_memory_mode,
        'reviewer_memory_mode': config.reviewer_memory_mode,
        'reviewer_metrics': LED.reviewer_metrics(changes),
    }
