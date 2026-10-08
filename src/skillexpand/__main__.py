import sys
import traceback

from skillexpand.cli import main
from skillexpand.reliability.errors import disposition
from skillexpand.reliability.units import exit_now

if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # noqa: BLE001 - report, then halt without joining workers
        traceback.print_exc()
        if not disposition(exc).retryable:
            exit_now(1)
        sys.exit(1)
    sys.exit(code)
