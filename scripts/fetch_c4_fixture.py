"""Download the original public C4 shard with resumable bounded ranges."""
import concurrent.futures
import json
import time
import urllib.request
from pathlib import Path

URL = 'https://huggingface.co/datasets/allenai/c4/resolve/1588ec454efa1a09f29cd18ddd04fe05fc8653a2/en/c4-train.00009-of-01024.json.gz'
SIZE = 318185592
BLOCK = 1024 * 1024
ROOT = Path(__file__).resolve().parents[2] / 'verifier-repair-20261013'


def fetch(start):
    end = min(start + BLOCK, SIZE) - 1
    part = ROOT / 'c4-parts' / str(start)
    if part.exists() and part.stat().st_size == end - start + 1:
        return
    for attempt in range(4):
        try:
            request = urllib.request.Request(URL, headers={'Range': f'bytes={start}-{end}'})
            with urllib.request.urlopen(request, timeout=120) as response:
                if response.headers.get('Content-Range') != f'bytes {start}-{end}/{SIZE}':
                    raise RuntimeError('Unexpected original-source range')
                data = response.read(BLOCK + 1)
            if len(data) != end - start + 1:
                raise RuntimeError('Incomplete original-source range')
            part.write_bytes(data)
            return
        except Exception:
            if attempt == 3:
                raise
            time.sleep(attempt + 1)


def main():
    (ROOT / 'c4-parts').mkdir(parents=True, exist_ok=True)
    starts = list(range(0, SIZE, BLOCK))
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(fetch, start) for start in starts]
        for count, future in enumerate(concurrent.futures.as_completed(futures), 1):
            future.result()
            if count % 20 == 0:
                print(json.dumps({'completed_ranges': count, 'total': len(starts)}), flush=True)
    target = ROOT / 'c4-original-shard-00009.json.gz'
    with target.open('wb') as output:
        for start in starts:
            output.write((ROOT / 'c4-parts' / str(start)).read_bytes())
    print(json.dumps({'original_fixture': str(target), 'bytes': target.stat().st_size}), flush=True)


if __name__ == '__main__':
    main()
