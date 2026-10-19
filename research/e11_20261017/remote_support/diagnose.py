"""Replay three frozen requests with the revised generic prompt, one call per case."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'code'))
from skillexpand.l2.editor import SkillEditor, validate_package_output
from skillexpand.runtime.llm_relay import relay_from_env, coalesce_sse

RUNS = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs')
SOURCES = [
    RUNS / 'tb21-e11-qwen-bare-262144-20261018-rationale-retry/generation_requests/0020-router.json',
    RUNS / 'tb21-e11-qwen-bare-262144-20261018-json-gate-retry/generation_requests/0016-router.json',
    RUNS / 'generic-skill-evolution-20261014/development/candidate-001/generation_attempts/001/request.json',
]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', type=int, choices=(1, 2, 3), required=True)
    args = parser.parse_args()
    case = ROOT / f'case-{args.case:02d}'
    case.mkdir(exist_ok=False)
    original = json.loads(SOURCES[args.case - 1].read_text())
    data = json.loads(original['messages'][1]['content'])
    prompt = SkillEditor(None, SimpleNamespace(version=0)).build_package_prompt(
        SimpleNamespace(**data['current_skill']), data['frozen_evidence'],
        data['development_observations'])
    assert json.loads(prompt[1].content) == data
    request = dict(original, messages=[{
        'role': 'system' if m.type == 'system' else 'user', 'content': m.content
    } for m in prompt])
    assert {k: v for k, v in request.items() if k != 'messages'} == {
        k: v for k, v in original.items() if k != 'messages'}
    write(case / 'source.json', {'request': str(SOURCES[args.case - 1]),
                                'max_calls': 1, 'retries': 0})
    write(case / 'original_request.json', original)
    write(case / 'request.json', request)
    live_env = json.load(sys.stdin)
    os.environ.update(live_env)
    frozen_env = json.loads((RUNS / 'tb21-e11-qwen-bare-262144-20261017/frozen/environment.json').read_text())
    os.environ.update({k: v for k, v in frozen_env.items() if v or k not in os.environ})
    started = time.time()
    relay = None
    try:
        relay = relay_from_env()
        relay.start()
        with (case / 'response.sse').open('wb') as saved:
            def chunks():
                for chunk in relay.transport.stream(request):
                    chunk = chunk.encode() if isinstance(chunk, str) else chunk
                    saved.write(chunk)
                    saved.flush()
                    yield chunk
            completion = coalesce_sse(chunks())
        write(case / 'completion.json', completion)
        raw = completion['choices'][0]['message']['content']
        (case / 'raw_response.txt').write_text(raw)
        value = validate_package_output(json.loads(raw))
        write(case / 'parsed.json', value)
        result = {'schema_passed': True, 'semantic_review': 'pending',
                  'no_change': value.get('no_change') is True,
                  'response_model': completion.get('model'),
                  'finish_reason': completion['choices'][0].get('finish_reason'),
                  'seconds': time.time() - started}
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        for secret in live_env.values():
            if secret:
                error = error.replace(secret, '[REDACTED]')
        result = {'schema_passed': False, 'validation_error': error,
                  'seconds': time.time() - started, 'stop': True}
    finally:
        if relay is not None:
            relay.close()
    write(case / 'result.json', result)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
