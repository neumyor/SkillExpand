"""Worker progress deadline and environment cleanup for pooled units."""
import math
import os
import warnings

from skillexpand.reliability.errors import InvalidInput


def close_environment(agent):
    """Cleanup failures must not replace the recorded execution outcome."""
    try:
        agent.env.close()
    except Exception as exc:
        warnings.warn(f'Environment cleanup failed: {type(exc).__name__}: {exc}', RuntimeWarning)


def worker_timeout():
    # Keep the outer pool deadline above TerminalBench's 7200-second task
    # allowance so long sandbox builds finish and report their verifier result
    # instead of being misclassified as worker timeouts.
    seconds = float(os.environ.get('EXPE_WORKER_TIMEOUT_SECONDS', '7500'))
    if not math.isfinite(seconds) or seconds <= 0:
        raise InvalidInput('EXPE_WORKER_TIMEOUT_SECONDS must be finite and positive')
    return seconds

