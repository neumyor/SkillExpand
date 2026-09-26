"""Probe recovery of the two full-campaign family discovery failures."""
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
    tags = tuple(S.from_dict(FD.TaskTag, read(path))
                 for path in sorted((search / 'tags').glob('*.json'),
                                    key=lambda path: int(path.stem)))
    representatives = FD.select_representatives(tags)
    records = sorted((read(path) for path in (search / 'requests').glob('*.json')
                      if 'proposing reusable SOP families' in read(path).get('input', '')),
                     key=lambda row: len(row['input']))
    replies = iter(row['output'] for row in records)
    proposed = FD.propose_families(representatives, lambda _: next(replies))
    expanded = FD.expand_proposals(proposed, representatives, tags)
    if any(len(item.candidate_task_ids) != len(tags) for item in expanded):
        raise ValueError('SearchQA candidate expansion omitted tasks')

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
    tag = FD.TaskTag(**read(directory / 'tags/34.json'))
    proposals = FD.parse_proposals(read(directory / 'proposals.json'), range(39))
    experience = S.from_dict(S.TaskExperience, read(directory / 'results/34.json'))
    card = projection(experience.experience_card)
    focused = tuple(item for item in proposals if item.family_id == 'family-p005')
    result = FD.audit_families((tag,), focused, ask, batch_size=1,
                               task_cards={34: card})[0]
    if result.family_id != 'family-p005':
        raise ValueError(f'ALFWorld task 34 assigned unexpected family {result.family_id}')
    output = {'searchqa_representatives': len(representatives),
              'searchqa_families': len(expanded),
              'searchqa_candidates_per_family': len(tags),
              'alfworld_task_34_family': result.family_id,
              'alfworld_task_34_rationale': result.rationale}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
