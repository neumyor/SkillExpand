"""Single-task dry run of the real ExpeL agent against the remote vLLM server.

This is the "small-sample dry run" that must pass before any benchmark-scale
measurement. It checks three things that a successful import cannot:

1.  the agent completes a full episode against the served model,
2.  an injected skill body actually reaches the prompt -- i.e. direct skill
    injection works, not merely that ``agent.rules`` was assigned,
3.  with ``fewshot_strategy='none'`` no training trajectory is retrieved.

Usage:
    source scripts/env.sh
    .venv/bin/python scripts/smoke_agent.py --task-id 8
"""

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
os.chdir(REPO_ROOT)

from skillexpand.runtime import agent_factory as F

#: A deliberately specific rule so its presence in the prompt is unambiguous.
SENTINEL_SKILL = (
    '1. SENTINEL-RULE: before heating anything, open the microwave and check it '
    'is empty.'
)


def hr(title: str) -> None:
    print(f'\n{"=" * 72}\n{title}\n{"=" * 72}', flush=True)


def prompt_text(agent) -> str:
    return '\n'.join(m.content for m in agent.prompt_history)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--task-id', type=int, default=8,
                    help='index into the benchmark task list')
    ap.add_argument('--skip-vanilla', action='store_true')
    ap.add_argument('--skip-skill', action='store_true')
    args = ap.parse_args()

    hr('0. config and backend')
    cfg = F.load_config('alfworld')
    fam = F.family_of(cfg, args.task_id)
    print(f'llm                 : {cfg.agent.llm}')
    print(f'base url            : {os.environ.get("EXPE_LLM_BASE_URL", "(unset -> OpenAI)")}')
    print(f'embedder_type       : {cfg.agent.retrieval_kwargs.embedder_type}')
    print(f'task {args.task_id}            : family={fam!r}')
    print(f'instruction         : {F.INIT_TASKS_FN["alfworld"](cfg)[args.task_id]["task"].split(chr(10))[-1]}')

    embedder = F.build_shared_embedder(cfg)
    t_agent = None

    results = {}

    if not args.skip_vanilla:
        hr(f'1. VANILLA arm (no rules, fewshot_strategy=none) on task {args.task_id}')
        t0 = time.time()
        agent = F.build_agent(cfg, task_idx=args.task_id, no_rules=True,
                              fewshot_strategy=F.FEWSHOT_NONE,
                              embedder_factory=embedder, max_reflection_depth=0)
        t_agent = time.time() - t0
        print(f'agent construction: {t_agent:.1f}s', flush=True)
        t0 = time.time()
        agent.run(mode='eval', eval_idx=args.task_id)
        elapsed = time.time() - t0
        text = prompt_text(agent)
        results['vanilla'] = dict(success=bool(agent.is_success()),
                                  steps=int(agent.curr_step), secs=elapsed,
                                  prompt_chars=len(text))
        print(f'\nresult: success={agent.is_success()} steps={agent.curr_step} '
              f'time={elapsed:.1f}s')
        print(f'prompt chars={len(text)}; contains SENTINEL-RULE: '
              f'{"SENTINEL-RULE" in text}')

    if not args.skip_skill:
        hr(f'2. CONSOLIDATED-DIRECT arm (skill injected, same task {args.task_id})')
        t0 = time.time()
        agent2 = F.build_agent(cfg, task_idx=args.task_id, rules=SENTINEL_SKILL,
                               no_rules=False, fewshot_strategy=F.FEWSHOT_NONE,
                               embedder_factory=embedder, max_reflection_depth=0)
        print(f'agent construction: {time.time() - t0:.1f}s', flush=True)
        t0 = time.time()
        agent2.run(mode='eval', eval_idx=args.task_id)
        elapsed = time.time() - t0
        text2 = prompt_text(agent2)
        injected = 'SENTINEL-RULE' in text2

        # Isolation: with fewshot_strategy='none' ExpeL returns from
        # update_dynamic_prompt_components before setup_vectorstore, so no
        # retrieval store should exist at all.  This is the crisp check that the
        # arm really is experience-isolated -- the static hand-written few-shots
        # are a different thing and remain present in every condition.
        retrieved_store_exists = hasattr(agent2, 'vectorstore')
        from skillexpand.runtime.prompts import FEWSHOTS
        static_fewshots = FEWSHOTS[cfg.benchmark.name][fam]

        results['skill'] = dict(success=bool(agent2.is_success()),
                                steps=int(agent2.curr_step), secs=elapsed,
                                injected=injected, prompt_chars=len(text2))
        print(f'\nresult: success={agent2.is_success()} steps={agent2.curr_step} '
              f'time={elapsed:.1f}s')
        print(f'prompt chars={len(text2)}; contains SENTINEL-RULE: {injected}')

        hr('3. mechanism and isolation verification')
        print(f'skill body reached the prompt        : {injected}')
        print(f'retrieval vectorstore was built      : {retrieved_store_exists} '
              '(must be False under fewshot_strategy=none)')
        print(f'fewshots present                     : {agent2.fewshots == static_fewshots}'
              ' -- equal to the STATIC family few-shots, not retrieved trajectories')
        print(f'  (static few-shots are hand-written upstream prompt examples '
              f'for {fam!r}, identical in every condition)')
        extra = len(text2) - (results.get('vanilla', {}).get('prompt_chars') or 0)
        if 'vanilla' in results:
            print(f'prompt grew by                       : {extra:+d} chars vs vanilla '
                  f'(the injected skill body)')

        failed = []
        if not injected:
            failed.append('rules were assigned but never injected -> the isolation '
                          'claim would be vacuous')
        if retrieved_store_exists:
            failed.append('a retrieval store was built under fewshot_strategy=none '
                          '-> the arm is not experience-isolated')
        if agent2.fewshots != static_fewshots:
            failed.append('fewshots differ from the static family examples -> '
                          'something was retrieved')
        if failed:
            print('\nFAIL:')
            for f in failed:
                print(f'  - {f}')
            return 1

    hr('SUMMARY')
    for arm, r in results.items():
        print(f'{arm:8s} success={r["success"]!s:5s} steps={r["steps"]:3d} '
              f'time={r["secs"]:.1f}s' +
              (f' injected={r["injected"]}' if 'injected' in r else ''))
    if t_agent is not None:
        print(f'\nper-agent construction: {t_agent:.1f}s '
              '(repeated for every task in the evolution loop)')
    print('\nDry run complete. The agent ran real episodes against the remote '
          'model.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
