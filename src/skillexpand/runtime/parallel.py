"""Generic bounded task pools; workers live with the layer that owns their unit."""
import multiprocessing
import os
from typing import Any, Callable, Dict, List, Optional, Sequence

from skillexpand.runtime import agent_factory as F
from skillexpand.reliability.errors import WorkerLost
from skillexpand.runtime.deadline import worker_timeout

#: Hard ceiling offered by the backend.  Kept explicit so a typo cannot ask for
#: 640 workers.
MAX_WORKERS = 256

#: Per-process caches.  A spawned worker re-imports this module, so anything
#: expensive belongs here rather than in every unit.
_CFG_CACHE: Dict[str, Any] = {}


def _config(benchmark: str):
    path=os.environ.get('EXPE_CONFIG_FILE','')
    key=(benchmark,path)
    if key not in _CFG_CACHE:
        from omegaconf import OmegaConf
        _CFG_CACHE[key] = OmegaConf.load(path) if path else F.load_config(benchmark)
    return _CFG_CACHE[key]


def effective_workers(requested: int, n_units: int) -> int:
    """Clamp a worker count to something sane for the batch."""
    if requested <= 0:
        return 1
    return max(1, min(int(requested), MAX_WORKERS, max(1, n_units)))


def run_generic(specs: Sequence[Any],
                worker: Callable[[Any], Dict[str, Any]],
                workers: int = 1,
                on_result: Optional[Callable[[Dict[str, Any]], None]] = None,
                chunksize: int = 1) -> List[Dict[str, Any]]:
    """Execute specs with an arbitrary module-level worker.

    ``worker`` must be a module-level function: macOS defaults to the ``spawn``
    start method, so closures and bound methods cannot cross the process boundary.

    Results are persisted as they finish; a slow first task cannot hold completed
    units in memory. Callers identify records by task ID, never arrival order.
    If ``on_result`` raises (a unit failure that halts the stage), queued units
    are cancelled and native worker processes are terminated.
    """
    specs = list(specs)
    n = effective_workers(workers, len(specs))
    results: List[Dict[str, Any]] = []

    native = any(getattr(spec, 'benchmark', None) == 'alfworld' and
                 getattr(spec, 'requires_native_environment', True) for spec in specs)
    if n <= 1 and not native:
        for spec in specs:
            record = worker(spec)
            results.append(record)
            if on_result:
                on_result(record)
        return results

    # Routing never constructs a native environment. It and SearchQA's private
    # in-memory indexes can share imports without sharing per-task state.
    if all(getattr(spec, 'benchmark', None) == 'searchqa' or
           not getattr(spec, 'requires_native_environment', True) for spec in specs):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        for benchmark in {spec.benchmark for spec in specs}:
            _config(benchmark)
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix='benchmark-task') as executor:
            futures = [executor.submit(worker, spec) for spec in specs]
            try:
                for future in as_completed(futures):
                    record = future.result()
                    results.append(record)
                    if on_result:
                        on_result(record)
            except BaseException:
                # Running threads finish their current request; nothing new starts.
                executor.shutdown(wait=False, cancel_futures=True)
                raise
        return results

    ctx = multiprocessing.get_context('spawn')
    timeout = worker_timeout()
    with ctx.Pool(processes=n, maxtasksperchild=1) as pool:
        # One unit per child allows reliable cleanup after native failures. A
        # progress deadline also catches crashed workers and uninterruptible C
        # calls that Python's environment alarm cannot interrupt.
        iterator = pool.imap_unordered(worker, specs, chunksize=1)
        for _ in specs:
            try:
                record = iterator.next(timeout=timeout)
            except multiprocessing.TimeoutError as exc:
                raise WorkerLost(f'No worker completed within {timeout:g}s; resume pending units') from exc
            results.append(record)
            if on_result:
                on_result(record)
    return results
