"""Capture provider channels without consuming formal reviewer cache/budgets."""
import argparse
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from skillexpand.runtime.llm_relay import relay_from_env, iter_sse_events


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--usage', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--last-start', action='store_true')
    parser.add_argument('--thinking-only', action='store_true')
    parser.add_argument('--omit-max-tokens', action='store_true')
    parser.add_argument('--model', default='deepseek-v4-flash-0731-tencent')
    args = parser.parse_args()
    args.out.mkdir(exist_ok=False)
    logging.basicConfig(filename=args.out / 'transport.log', level=logging.INFO)
    rows = [json.loads(line) for line in args.usage.read_text().splitlines()]
    starts = [r['prompts'][0] for r in rows if r['event'] == 'start']
    prompt = starts[-1] if args.last_start else starts[0]
    assert prompt.startswith('Human: ')
    prompt = prompt[len('Human: '):]
    relay = relay_from_env()
    def probe(thinking):
        prefix = args.out / ('thinking' if thinking else 'no-thinking')
        payload = {'model': args.model, 'stream': True,
                   'temperature': 0, 'enable_thinking': thinking,
                   'messages': [{'role': 'user', 'content': prompt}]}
        if not args.omit_max_tokens:
            payload['max_tokens'] = 32768
        prefix.with_suffix('.request.json').write_text(json.dumps(payload))
        started = time.monotonic()
        content, reasoning, finish, usage = [], [], [], []
        count = 0
        report = {'thinking': thinking, 'diagnostic_only': True,
                  'max_tokens_sent': 'max_tokens' in payload}
        try:
            with prefix.with_suffix('.sse').open('wb') as raw:
                def capture():
                    for chunk in relay.transport.stream(payload):
                        raw.write(chunk.encode() if isinstance(chunk, str) else chunk)
                        raw.flush()
                        yield chunk
                for event in iter_sse_events(capture()):
                    count += 1
                    if event.get('usage'):
                        usage.append(event['usage'])
                    for choice in event.get('choices', []):
                        delta = choice.get('delta') or {}
                        if delta.get('content'):
                            content.append(delta['content'])
                        if delta.get('reasoning_content'):
                            reasoning.append(delta['reasoning_content'])
                        if choice.get('finish_reason'):
                            finish.append(choice['finish_reason'])
            text, thoughts = ''.join(content), ''.join(reasoning)
            prefix.with_suffix('.content.txt').write_text(text)
            prefix.with_suffix('.reasoning.txt').write_text(thoughts)
            parsed = None
            error = None
            from skillexpand.evaluation.validation import PredictedSkillScorer
            from types import SimpleNamespace
            scorer = PredictedSkillScorer(SimpleNamespace(benchmark=SimpleNamespace(name='terminalbench')),
                SimpleNamespace(fingerprint='diagnostic'), None)
            try:
                parsed = scorer._parse(text)
            except Exception as exc:
                error = type(exc).__name__ + ': ' + str(exc)
            report.update(status='complete_stream', events=count, content_chars=len(text),
                reasoning_chars=len(thoughts), finish_reasons=finish, usage=usage,
                parsed_content=parsed, parse_error=error, legacy_client_chars=len(text),
                content_tail=text[-250:], reasoning_tail=thoughts[-350:])
            if not text:
                report['error_class'] = ('provider_output_truncated' if 'length' in finish
                                         else 'provider_empty_content')
        except Exception as exc:
            report.update(status='error', error_class=getattr(exc, 'error_class', type(exc).__name__),
                          error=str(exc)[:1000], events=count,
                          content_chars=sum(map(len, content)),
                          reasoning_chars=sum(map(len, reasoning)), finish_reasons=finish, usage=usage)
            prefix.with_suffix('.content.txt').write_text(''.join(content))
            prefix.with_suffix('.reasoning.txt').write_text(''.join(reasoning))
        report['elapsed_seconds'] = time.monotonic() - started
        prefix.with_suffix('.result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(probe, (True,) if args.thinking_only else (True, False)))
    finally:
        # The diagnostic uses transport directly; the HTTP server was never
        # started, so calling server.shutdown() would wait indefinitely.
        relay.transport.close()
        relay.server.server_close()


if __name__ == '__main__':
    main()
