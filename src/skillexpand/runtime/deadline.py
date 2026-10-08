"""Bound native environment calls on macOS/Linux worker main threads."""
import functools
import math
import os
import signal
import threading
import warnings


def close_environment(agent):
    """Cleanup failures must not replace the recorded execution outcome."""
    try:
        agent.env.close()
    except Exception as exc:
        warnings.warn(f'Environment cleanup failed: {type(exc).__name__}: {exc}', RuntimeWarning)


def worker_timeout():
    # Keep the outer pool deadline above Terminal-Bench's 7200-second task
    # allowance so long native builds can finish and report their verifier
    # result instead of being misclassified as worker timeouts.
    seconds = float(os.environ.get('EXPE_WORKER_TIMEOUT_SECONDS', '7500'))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('EXPE_WORKER_TIMEOUT_SECONDS must be finite and positive')
    return seconds


def environment_call(function):
    @functools.wraps(function)
    def bounded(*args, **kwargs):
        seconds = float(os.environ.get('EXPE_ENV_TIMEOUT_SECONDS', '120'))
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('EXPE_ENV_TIMEOUT_SECONDS must be finite and positive')
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('Native environment deadlines require a process main thread')
        def expired(signum, frame):
            raise TimeoutError(f'Environment {function.__name__} exceeded {seconds:g}s')
        previous = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return function(*args, **kwargs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    return bounded
