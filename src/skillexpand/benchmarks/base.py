import functools
import math
import os
import signal
import threading
from abc import abstractmethod
from typing import Dict, Any
import gym

from skillexpand.reliability.errors import (
    EnvironmentFailure, EnvironmentTimeout, InvalidInput, register_translation,
)

# Native environments run in subprocesses and pipes; losing them is not the agent's doing.
for _error in (BrokenPipeError, ConnectionResetError, EOFError):
    register_translation(_error, EnvironmentFailure)

class BaseEnv(gym.Env):
    @abstractmethod
    def reset(self):
        pass

    @abstractmethod
    def step(self, action: str, *args, **kwargs) -> Dict[str, Any]:
        pass

    @abstractmethod
    def success_fn(self) -> bool:
        pass

    def is_terminated(self) -> bool:
        return self.terminated

    def is_truncated(self) -> bool:
        return self.curr_step > self.max_steps


def environment_call(function):
    @functools.wraps(function)
    def bounded(*args, **kwargs):
        seconds = float(os.environ.get('EXPE_ENV_TIMEOUT_SECONDS', '120'))
        if not math.isfinite(seconds) or seconds <= 0:
            raise InvalidInput('EXPE_ENV_TIMEOUT_SECONDS must be finite and positive')
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('Native environment deadlines require a process main thread')
        def expired(signum, frame):
            raise EnvironmentTimeout(f'Environment {function.__name__} exceeded {seconds:g}s')
        previous = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return function(*args, **kwargs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    return bounded
