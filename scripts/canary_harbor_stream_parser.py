"""Live Tencent relay canary using the repaired Harbor SSE parser."""
import ast
import json
import os
from pathlib import Path
from skillexpand.runtime.llm_relay import TencentSandboxTransport

adapter = Path('/data2/liyishan/tb21-tencent-skill/python/tencent_sandbox_terminus.py')
function = next(n for n in ast.parse(adapter.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == 'read_streamed_completion')
namespace = {}
exec(compile(ast.Module(body=[function], type_ignores=[]), str(adapter), 'exec'), namespace)
out = Path('/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/tb21-20261007-harbor-parser-canary')
out.mkdir(exist_ok=False)
transport = None
reports = []
try:
    transport = TencentSandboxTransport(template='sdt-28lb4d9k', api_key=os.environ['OPENAI_API_KEY'],
        api_base='https://llm-center.modelbest.co/v1', metadata={'x-mounts': '[]'}, request_timeout=300)
    for model in ('qwen3.6-flash-distill', 'deepseek-v4-flash-0731-tencent'):
        def lines():
            pending = b''
            for chunk in transport.stream({'model': model, 'stream': True,
                    'messages': [{'role': 'user', 'content': 'Reply exactly: hello'}],
                    'enable_thinking': True, 'max_tokens': 32768}):
                chunk = chunk.encode() if isinstance(chunk, str) else chunk
                with (out / (model + '.sse')).open('ab') as stream:
                    stream.write(chunk)
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    yield line + b'\n'
            if pending:
                yield pending
        response = namespace['read_streamed_completion'](lines())
        assert response['choices'][0]['message']['content'].strip() == 'hello'
        report = {'model': model, 'status': 'ok', 'stream_metrics': response['stream_metrics'],
                  'finish_reason': response['choices'][0]['finish_reason']}
        reports.append(report)
        (out / 'result.json').write_text(json.dumps(reports, indent=2) + '\n')
    (out / 'status.json').write_text(json.dumps({'status': 'complete', 'experimental_task_attempts': 0,
        'harbor_parser_live_canary': True, 'all_harbor_call_lifecycle_tested': False}, indent=2) + '\n')
    print(json.dumps(reports))
except Exception as exc:
    (out / 'status.json').write_text(json.dumps({'status': 'needs_attention', 'error': str(exc),
        'error_class': getattr(exc, 'error_class', type(exc).__name__)}, indent=2) + '\n')
    raise
finally:
    if transport is not None:
        transport.close()
