"""Probe the fresh family taxonomy and forced-choice assignment artifacts."""
import argparse
import json
from pathlib import Path

from langchain.schema import HumanMessage
from omegaconf import OmegaConf

from skillexpand import schema as S
from skillexpand.l1 import family_discovery as FD
from skillexpand.l1.adapters import resolve
from skillexpand.l1.protocol import projection
from skillexpand.runtime import agent_factory as F


def read(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()

    search = root / 'full/searchqa/run/discovery'
    tag_paths = sorted((search / 'tags').glob('*.json'), key=lambda path: int(path.stem))
    tags = FD.parse_tags({'tags': [read(path) for path in tag_paths]},
                         [int(path.stem) for path in tag_paths])
    proposals = FD.parse_proposals(read(search / 'proposals.json'))
    assignment_paths = sorted((search / 'assignments').glob('*.json'),
                              key=lambda path: int(path.stem))
    assignments = FD.parse_assignments(
        {'assignments': [read(path) for path in assignment_paths]},
        [tag.task_id for tag in tags], proposals)
    if any('candidate_task_ids' in proposal.to_dict() or
           'exclusion_criteria' in proposal.to_dict() for proposal in proposals):
        raise ValueError('SearchQA proposal contains removed membership fields')
    if {item.task_id for item in assignments} != {tag.task_id for tag in tags}:
        raise ValueError('SearchQA assignments do not cover every task')

    alf = root / 'full/alfworld/run'
    cfg = OmegaConf.load(alf / 'config.json')
    adapter = resolve(cfg)
    context = {
        'benchmark': 'alfworld',
        'execution_instructions': adapter.execution_instructions or F.SYSTEM_INSTRUCTION['alfworld'],
        'tool_semantics': getattr(adapter, 'tool_semantics', ''),
        'family_contract': FD.FAMILY_CONTRACT,
        'evidence_policy': ('The execution field is an observed trace excerpt. '
                            'Infer operations from actual actions and feedback, not imagined solutions. '
                            'Omitted actions are not absent actions; success does not validate every '
                            'intermediate action. Assisted traces do not establish autonomous ability.'),
    }
    host = F.build_reasoning_host(cfg, root / 'preflight/family-recovery-34.usage.json')

    def ask(prompt):
        value = host.llm([HumanMessage(content=(
            'BENCHMARK RUNTIME CONTEXT (use its actual tools and completion semantics):\n'
            + json.dumps(context, ensure_ascii=False) + '\n\n' + prompt))],
            stop=[], replace_newline=False)
        return value

    directory = alf / 'discovery'
    tag = FD.parse_tags({'tags': [read(directory / 'tags/34.json')]}, [34])[0]
    proposals = FD.parse_proposals(read(directory / 'proposals.json'))
    experience = S.from_dict(S.TaskExperience, read(directory / 'results/34.json'))
    assignment = FD.assign_families(
        (tag,), proposals, ask, batch_size=1,
        task_cards={34: projection(experience.experience_card)},
    )[0]
    if assignment.family_id != 'family-p005':
        raise ValueError(f'ALFWorld task 34 assigned unexpected family {assignment.family_id}')
    output = {
        'searchqa_tasks': len(tags),
        'searchqa_families': len(proposals),
        'searchqa_assignments': len(assignments),
        'alfworld_task_34_family': assignment.family_id,
        'alfworld_task_34_match_type': assignment.match_type,
        'alfworld_task_34_rationale': assignment.rationale,
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
