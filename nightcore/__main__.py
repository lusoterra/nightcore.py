"""Module entry point: python -m nightcore."""

import sys

from . import util
from .cli import main


try:
    sys.exit(main())
except KeyboardInterrupt:
    # Second Ctrl+C (or an interrupt outside the tracked tools):
    # partial outputs go, tempdirs already cleaned themselves.
    for partial in util._active_outputs:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
    print("\nInterrupted.", file=sys.stderr)
    sys.exit(130)
