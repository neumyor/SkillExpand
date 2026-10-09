"""Task loading and environments for the supported benchmarks."""
import json
from pathlib import Path
import joblib
from .base import BaseEnv
from .searchqa import QAEnv
from .alfworld import AlfworldEnv
from .terminalbench import TerminalBenchEnv, load_tasks as _terminalbench_tasks
from .alfworld import get_env_name_from_gamefile


def _alfworld_tasks(cfg):
    return [
        {'task': f'{cfg.benchmark.task_prefix}{row["goal"]}',
         'env_kwargs': {'config': cfg.benchmark, 'gamefile': row['gamefile']},
         'env_name': get_env_name_from_gamefile(row['gamefile'])}
        for row in json.loads(Path(cfg.benchmark.task_file).read_text())
    ]


__all__ = ['BaseEnv', 'QAEnv', 'AlfworldEnv', 'ENVS', 'INIT_TASKS_FN']

INIT_TASKS_FN = {'searchqa': lambda cfg: _searchqa_tasks(cfg), 'alfworld': _alfworld_tasks,
                 'terminalbench': _terminalbench_tasks}
ENVS = {'searchqa': QAEnv, 'alfworld': AlfworldEnv, 'terminalbench': TerminalBenchEnv}


def _searchqa_tasks(cfg):
    """Normalize common SearchQA JSON/JSONL/joblib rows for the QA environment."""
    path = cfg.benchmark.task_file
    if str(path).endswith(('.joblib', '.pkl')):
        rows = joblib.load(path)
        rows = rows.to_dict('records') if hasattr(rows, 'to_dict') else rows
    else:
        text = Path(path).read_text()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            raw = [json.loads(line) for line in text.splitlines() if line.strip()]
        rows = raw.get('data', raw) if isinstance(raw, dict) else raw
    out = []
    for row in rows:
        question = row.get('question') or row.get('query') or row.get('task')
        answers = row.get('answers', row.get('answer', row.get('key', '')))
        if isinstance(answers, (list, tuple)):
            answer = answers[0] if answers else ''
        else:
            answer = answers
        if not question or not answer:
            raise ValueError('SearchQA rows require question and answer(s)')
        out.append({
            'task': f'{getattr(cfg.benchmark, "task_prefix", "")}{question}',
            'env_kwargs': {'question': question, 'key': answer,
                           'context': row.get('context')},
            'env_name': 'searchqa',
        })
    return out
