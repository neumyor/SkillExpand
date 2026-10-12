"""Launch the independent E8 smoke and gated bare 267-trial job."""
import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand')
ROOT = Path('/data2/liyishan/tbench2-openclaw-min/jobs/tb21_skillexpand_20261012')
REG = REPO / 'runs/tb21-e8-deepseek-max-1m-20261012'
ADAPTER = Path('/data2/liyishan/tb21-tencent-skill/python')
PYTHON = '/data2/liyishan/tbench2-openclaw-min/.venv/bin/python'
FULL = 'tb21_e8_deepseek_max_1m_raw_20261012'
SMOKE = 'tb21_e8_deepseek_max_1m_smoke_20261012'


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def prepare():
    source = Path('/data2/liyishan/tbench2-openclaw-min/jobs/tb21_skillexpand_20261002/tb21_e2_deepseek_raw_20261002/config.json')
    cfg = json.loads(source.read_text())
    cfg.update(job_name=FULL, jobs_dir=str(ROOT))
    kwargs = cfg['agents'][0]['kwargs']
    kwargs['model_info'] = {'max_input_tokens': 1048576, 'max_output_tokens': 65536}
    kwargs.update(enable_summarize=True, proactive_summarization_threshold=8000)
    kwargs['llm_call_kwargs']['extra_body']['reasoning_effort'] = 'max'
    write(REG / 'full_config.json', cfg)
    subprocess.run([PYTHON, '-c', 'import json,sys; from harbor.models.job.config import JobConfig; JobConfig.model_validate(json.load(open(sys.argv[1])))', str(REG / 'full_config.json')], check=True)
    smoke = json.loads(json.dumps(cfg))
    smoke.update(job_name=SMOKE, n_attempts=1)
    smoke['orchestrator']['n_concurrent_trials'] = 1
    smoke['datasets'][0]['task_names'] = ['fix-git']
    write(REG / 'smoke_config.json', smoke)
    write(REG / 'manifest.json', {'stage': 'E8', 'workers': 100, 'source_config': str(source),
        'model': cfg['agents'][0]['model_name'], 'skill': None,
        'context_tokens': 1048576, 'reasoning_effort': 'max', 'formal_trials': 267})


def run(config, phase):
    write(REG / 'status.json', {'stage': 'E8', 'status': 'running', 'pid': os.getpid(),
        'workers': 100, 'phase': phase})
    with (REG / (phase + '.log')).open('a') as log:
        child = subprocess.Popen([PYTHON, str(ADAPTER / 'run_tencent_job.py'),
            '--config', str(REG / config), '--retry-archive-root', str(ROOT / 'e8_retry_archives'),
            '--no-export-traces', '--fail-fast'], stdout=log, stderr=subprocess.STDOUT)
        write(REG / 'child.json', {'pid': child.pid, 'phase': phase})
        if child.wait():
            raise RuntimeError(phase + ' runner failed; inspect retained log')


def gate():
    results = list((ROOT / SMOKE).glob('*/result.json'))
    if len(results) != 1:
        raise ValueError('Smoke requires one completed trial')
    result = json.loads(results[0].read_text())
    reward = (result.get('verifier_result') or {}).get('rewards', {}).get('reward')
    if reward not in (0, 1) or result.get('exception_info'):
        raise ValueError('Smoke has no healthy real verifier outcome')
    trial = results[0].parent
    requests = [json.loads(line) for line in (trial / 'agent/raw_model_requests.jsonl').read_text().splitlines()]
    if not requests or any(v.get('reasoning_effort') != 'max' or
            v.get('model') != 'deepseek-v4-flash-0731' or v.get('max_tokens') != 65536 for v in requests):
        raise ValueError('Actual HTTP parameters mismatch')
    if not json.loads((trial / 'agent/trajectory.json').read_text()):
        raise ValueError('Missing smoke trajectory')
    cfg = json.loads((REG / 'smoke_config.json').read_text())['agents'][0]
    subprocess.run([PYTHON, '-c', 'import json,sys; from pathlib import Path; from tencent_sandbox_terminus import TencentSandboxTerminus2; c=json.load(open(sys.argv[1]))["agents"][0]; a=TencentSandboxTerminus2(model_name=c["model_name"],logs_dir=Path(sys.argv[2]),**c["kwargs"]); assert a._tencent_model_info["max_input_tokens"]==1048576', str(REG / 'smoke_config.json'), str(trial / 'agent')], check=True)
    write(REG / 'smoke_audit.json', {'passed': True, 'reward': reward,
        'trial': str(trial), 'actual_requests': len(requests), 'configured_input_tokens': 1048576,
        'provider_full_context_or_effort_not_proven': True})


def worker():
    from run_tb21_aligned import reserve
    try:
        reserve(REG, 'E8', 100)
        prepare()
        run('smoke_config.json', 'smoke')
        gate()
        run('full_config.json', 'full')
        write(REG / 'status.json', {'stage': 'E8', 'status': 'needs_audit', 'phase': 'full_finished'})
    except Exception as exc:
        write(REG / 'status.json', {'stage': 'E8', 'status': 'needs_attention',
            'error_class': type(exc).__name__, 'reason': str(exc)[:500]})
        raise


if __name__ == '__main__':
    if '--worker' in sys.argv:
        worker()
    else:
        if REG.exists():
            raise SystemExit('E8 registration exists; inspect before any recovery')
        env = os.environ.copy()
        original = dict(v.split('=', 1) for v in Path('/proc/2320647/environ').read_bytes().decode().split('\0') if '=' in v)
        for key in ['E2B_API_KEY', 'OPENAI_API_KEY', 'TB21_METHOD_API_KEY']:
            if original.get(key):
                env[key] = original[key]
        env['OPENAI_API_KEY'] = original.get('TB21_METHOD_API_KEY') or original['OPENAI_API_KEY']
        env.update(PYTHONPATH=str(ADAPTER) + ':/data2/liyishan/tbench2-openclaw-min/framework/python:' + str(REPO / 'src') + ':' + str(REPO / 'scripts'),
            E2B_DOMAIN='ap-beijing.tencentags.com', TBENCH_PERSIST_SANDBOXES='0',
            TB21_TOTAL_WORKER_LIMIT='250', TB21_PIPELINE_CACHE='1',
            TB21_PIPELINE_CACHE_VERSION='pipeline-uv095-py3139-v6')
        REG.mkdir(parents=True)
        ROOT.mkdir(parents=True, exist_ok=True)
        with (REG / 'supervisor.log').open('a') as log:
            p = subprocess.Popen([sys.executable, __file__, '--worker'], env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        print(json.dumps({'pid': p.pid, 'registration': str(REG)}))
