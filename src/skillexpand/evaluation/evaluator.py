"""Execution termination classification."""
from typing import Optional

def classify_failure(success: bool, terminated: bool, truncated: bool,
                     steps: int, max_steps: int,
                     error: Optional[str] = None) -> Optional[str]:
    """Single source of truth for failure modes, shared with the baseline script."""
    if success:
        return None
    if error:
        return 'execution_error'
    if terminated and not truncated and steps < max_steps:
        return 'environment_terminated'
    if truncated:
        return 'max_steps'
    return 'stalled'
