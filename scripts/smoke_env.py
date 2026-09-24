"""Smoke-test the ALFWorld stack end to end, and measure env construction cost.

This exists because installing packages proves nothing: the upstream repo ships
without its dataset, and ExpeL's env adapter is written against an alfworld
release that no longer exists on PyPI. Both had to be fixed before a single task
could run, so "it imports" is not evidence.

Usage:
    .venv/bin/python scripts/smoke_env.py [--steps N]
"""

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
os.chdir(REPO_ROOT)

# alfworld reads this for its own downloaded-asset lookups. Our restored tree
# already matches the layout alfworld-download would have produced.
os.environ.setdefault('ALFWORLD_DATA', str(REPO_ROOT / 'data' / 'alfworld'))

from omegaconf import OmegaConf  # noqa: E402

BENCHMARK = 'alfworld'
TASK_FILE = REPO_ROOT / 'data' / 'alfworld' / 'alfworld_tasks_suffix.json'


def hr(title: str) -> None:
    print(f'\n{"=" * 72}\n{title}\n{"=" * 72}', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--steps', type=int, default=3,
                        help='how many environment steps to take')
    args = parser.parse_args()

    hr('0. load benchmark config and task list')
    cfg = OmegaConf.load(REPO_ROOT / 'src' / 'skillexpand' / 'configs' / 'benchmark' / 'alfworld.yaml')
    import json
    tasks = json.load(open(cfg.task_file, 'r'))
    print(f'task_file      : {cfg.task_file}')
    print(f'tasks          : {len(tasks)}')
    print(f'split          : {cfg.split}')
    print(f'data_path(train): {cfg.dataset.data_path}')
    print(f'eval_ood_path  : {cfg.dataset.eval_ood_data_path}')
    missing = [t for t in tasks if not os.path.exists(t['gamefile'])]
    print(f'gamefiles      : {len(tasks) - len(missing)}/{len(tasks)} resolvable')
    if missing:
        print(f'FATAL: first missing gamefile -> {missing[0]["gamefile"]}')
        return 1

    hr('1. construct AlfredTWEnv directly (measures the collect_game_files walk)')
    from alfworld.agents.environment.alfred_tw_env import AlfredTWEnv

    t0 = time.time()
    main_env = AlfredTWEnv(cfg, train_eval=cfg.split)
    t_build = time.time() - t0
    print(f'\n>>> AlfredTWEnv(config, train_eval={cfg.split!r}) took {t_build:.1f}s, '
          f'collected {main_env.num_games} games', flush=True)

    # Isolate the walk: collect_game_files is what os.walk + per-task JSON
    # parsing happens in, and ExpeL throws its result away one line later.
    t0 = time.time()
    main_env.collect_game_files()
    t_walk = time.time() - t0
    print(f'>>> collect_game_files() alone took {t_walk:.1f}s', flush=True)

    hr('2. initialise the textworld gym env on one real gamefile')
    gamefile = tasks[0]['gamefile']
    print(f'gamefile: {gamefile}')
    main_env.game_files = [gamefile]  # this is exactly what ExpeL does post-init
    t0 = time.time()
    env = main_env.init_env(batch_size=1)
    print(f'init_env took {time.time() - t0:.1f}s', flush=True)

    t0 = time.time()
    obs, infos = env.reset()
    print(f'reset took {time.time() - t0:.1f}s', flush=True)
    print(f'\nobservation:\n{obs[0][:600]}')

    adm = infos.get('admissible_commands', [[]])[0]
    print(f'\nadmissible commands ({len(adm)}): {adm[:10]}')

    hr(f'3. take {args.steps} steps with admissible actions')
    done = False
    for i in range(args.steps):
        if not adm:
            print('no admissible commands; stopping')
            break
        action = adm[0]
        obs, reward, done, info = env.step([action])
        print(f'step {i + 1}: action={action!r} reward={reward} done={done} '
              f'won={info.get("won")}')
        print(f'  -> {obs[0][:200]}')
        adm = info.get('admissible_commands', [[]])[0] if not done else []
        if done:
            break

    hr('4. exercise ExpeL\'s AlfworldEnv wrapper (the code path the agent uses)')
    from skillexpand.benchmarks.alfworld import AlfworldEnv
    from skillexpand.benchmarks.alfworld import resolve_alfworld_env_cls

    print(f'resolved env class: {resolve_alfworld_env_cls(cfg.env.type).__name__}')
    t0 = time.time()
    wrapper = AlfworldEnv(gamefile=gamefile, config=cfg, max_steps=cfg.max_steps)
    t_wrapper = time.time() - t0
    print(f'\n>>> AlfworldEnv(...) took {t_wrapper:.1f}s')
    print(f'    env_name (skill family) = {wrapper.env_name!r}')
    print(f'    task prompt             = {wrapper.task!r}')
    print(f'    success_fn()            = {wrapper.success_fn()}')

    hr('SUMMARY')
    print(f'AlfredTWEnv construction      : {t_build:.1f}s')
    print(f'  of which collect_game_files : {t_walk:.1f}s  ({t_walk / t_build * 100:.0f}%)')
    print(f'AlfworldEnv wrapper per task  : {t_wrapper:.1f}s')
    print()
    print('ExpeL rebuilds AlfworldEnv for EVERY task, then immediately overwrites')
    print('main_env.game_files with a single file -- so the walk above is repeated')
    print('and discarded once per task. At the measured cost that is a fixed')
    print('per-task tax on every experiment in the evolution loop.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
