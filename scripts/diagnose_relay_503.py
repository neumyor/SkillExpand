"""Probe relay exception origins without issuing experimental reviewer requests."""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from skillexpand.runtime.llm_relay import TencentSandboxTransport


def main():
    out = Path(os.environ['TB21_DIAGNOSTIC_RUN'])
    out.mkdir(exist_ok=False)
    logging.basicConfig(filename=out / 'transport.log', level=logging.INFO)
    transport = TencentSandboxTransport(template='sdt-28lb4d9k',
        api_key=os.environ['OPENAI_API_KEY'], api_base='https://llm-center.modelbest.co/v1',
        metadata={'x-mounts': '[]'}, request_timeout=300)
    def one(index):
        started = time.monotonic()
        try:
            payload = {'model': 'deepseek-v4-flash-0731-tencent',
                'stream': True, 'messages': [{'role': 'user', 'content': 'Reply exactly: hello'}],
                'enable_thinking': True}
            if os.environ.get('TB21_DIAGNOSTIC_SCHEMA') == '1':
                from skillexpand.evaluation.validation import PredictedSkillScorer
                payload['response_format'] = PredictedSkillScorer.response_format()
                payload['messages'][0]['content'] = ('Return JSON: probability_true=0.8, '
                    'predicted_success=true, reason="diagnostic"')
            if os.environ.get('TB21_DIAGNOSTIC_USAGE'):
                records = [json.loads(line) for line in Path(os.environ['TB21_DIAGNOSTIC_USAGE']).read_text().splitlines()]
                prompt = next(r['prompts'][0] for r in records if r.get('event') == 'start')
                assert prompt.startswith('Human: ')
                payload['messages'][0]['content'] = prompt[len('Human: '):]
            result = transport.request(payload)
            return {'index': index, 'status': 'ok', 'elapsed': time.monotonic() - started,
                    'content': result['choices'][0]['message']['content']}
        except Exception as exc:
            chain = []
            current = exc
            while current is not None:
                detail = str(current).replace(os.environ['OPENAI_API_KEY'], '[REDACTED]')
                chain.append({'type': type(current).__name__, 'detail': detail[:2000]})
                current = current.__cause__
            return {'index': index, 'status': 'error', 'elapsed': time.monotonic() - started,
                    'error_class': getattr(exc, 'error_class', None), 'chain': chain}
    try:
        reports = [one(0)]
        (out / 'result.json').write_text(json.dumps(reports, indent=2))
        if not os.environ.get('TB21_DIAGNOSTIC_USAGE'):
            with ThreadPoolExecutor(max_workers=5) as pool:
                reports.extend(pool.map(one, range(1, 6)))
        (out / 'result.json').write_text(json.dumps(reports, indent=2))
        print(json.dumps(reports))
    finally:
        transport.close()


if __name__ == '__main__':
    main()
