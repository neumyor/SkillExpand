"""Compare in-sandbox provider bytes with E2B stdout, outside formal scoring."""
import argparse
import asyncio
import json
import shlex
from pathlib import Path

from skillexpand.runtime.llm_relay import relay_from_env


PROBE = r'''
import json, os, sys, time, urllib.request
from pathlib import Path
root = Path('/tmp/sse-transport-probe')
payload = (root / 'request.json').read_bytes()
report = {'started': time.time(), 'diagnostic_only': True}
request = urllib.request.Request(os.environ['MODEL_API_BASE'].rstrip('/') + '/chat/completions',
    data=payload, headers={'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY'],
    'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(request, timeout=1800) as response:
        report['http_status'] = response.status
        report['headers'] = {k: v for k, v in response.headers.items()
            if k.lower() not in ('set-cookie', 'authorization', 'proxy-authorization')}
        report['http_opened'] = time.time()
        with (root / 'provider.sse').open('wb') as raw:
            for line in response:
                raw.write(line)
                raw.flush()
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
        report['normal_http_eof'] = True
        report['http_remaining_length'] = getattr(response, 'length', None)
except Exception as exc:
    report['error_type'] = type(exc).__name__
    report['error'] = str(exc).replace(os.environ['OPENAI_API_KEY'], '[REDACTED]')
finally:
    report['ended'] = time.time()
    report['elapsed_seconds'] = report['ended'] - report['started']
    (root / 'http.json').write_text(json.dumps(report, indent=2))
    print('SSE_PROBE_END ' + json.dumps(report), file=sys.stderr, flush=True)
'''


def inspect(raw):
    events, errors, finish, ids = 0, [], [], set()
    content = reasoning = 0
    for line in raw.splitlines():
        if not line.startswith(b'data:'):
            continue
        data = line[5:].strip()
        if data == b'[DONE]':
            continue
        try:
            event = json.loads(data)
        except ValueError:
            errors.append({'invalid_json': data.decode(errors='replace')[:200]})
            continue
        events += 1
        if event.get('id'):
            ids.add(event['id'])
        if event.get('error'):
            errors.append(event['error'])
        for choice in event.get('choices', []):
            delta = choice.get('delta') or {}
            content += len(delta.get('content') or '')
            reasoning += len(delta.get('reasoning_content') or '')
            if choice.get('finish_reason') is not None:
                finish.append(choice['finish_reason'])
    return {'bytes': len(raw), 'events': events, 'done': b'data: [DONE]' in raw,
            'content_chars': content, 'reasoning_chars': reasoning,
            'finish_reasons': finish, 'errors': errors, 'provider_ids': sorted(ids)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(exist_ok=False, parents=True)
    payload = json.loads(args.request.read_text())
    assert payload['stream'] and payload['enable_thinking']
    assert 'max_tokens' not in payload and 'response_format' not in payload
    (args.out / 'request.json').write_text(json.dumps(payload, ensure_ascii=False))
    relay = relay_from_env()
    transport = relay.transport
    (args.out / 'manifest.json').write_text(json.dumps({
        'diagnostic_only': True, 'formal_cache_import': False,
        'sandbox_id': transport.sandbox_id, 'workers': 1,
        'request_source': str(args.request)}, indent=2))

    async def run():
        sandbox = transport._sandbox
        root = '/tmp/sse-transport-probe'
        await sandbox.commands.run('mkdir -p ' + root, user='root')
        await sandbox.files.write(root + '/request.json', json.dumps(payload), user='root')
        report = {}
        with (args.out / 'host.sse').open('wb') as capture:
            def emit(chunk):
                capture.write(chunk.encode() if isinstance(chunk, str) else chunk)
                capture.flush()
            handle = await sandbox.commands.run('python3 -u -c ' + shlex.quote(PROBE),
                user='root', background=True, timeout=0, on_stdout=emit)
            try:
                result = await handle.wait()
                report['exit_code'] = result.exit_code
                (args.out / 'command.stderr').write_text(result.stderr)
                (args.out / 'command.stdout').write_text(result.stdout)
            except Exception as exc:
                report.update(command_error_type=type(exc).__name__,
                              exit_code=getattr(exc, 'exit_code', None))
                (args.out / 'command.stderr').write_text(getattr(exc, 'stderr', '') or '')
        for filename in ('provider.sse', 'http.json'):
            data = await sandbox.files.read(root + '/' + filename, user='root')
            (args.out / filename).write_text(data)
        host = (args.out / 'host.sse').read_bytes()
        provider = (args.out / 'provider.sse').read_bytes()
        report.update(host=inspect(host), provider=inspect(provider),
                      identical=host == provider,
                      http=json.loads((args.out / 'http.json').read_text()))
        (args.out / 'result.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
    try:
        asyncio.run_coroutine_threadsafe(run(), transport._loop).result(timeout=1900)
    finally:
        transport.close()
        relay.server.server_close()
        (args.out / 'cleanup.json').write_text(json.dumps({'sandbox_deleted': True}))


if __name__ == '__main__':
    main()
