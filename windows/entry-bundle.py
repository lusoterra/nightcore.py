"""PyInstaller entry point: absolute-import twin of nightcore/__main__.py.

__main__.py uses a relative import (from . import util), which fails when
PyInstaller runs it as a top-level script. This does the same thing with
absolute imports: run main(), clean partials on Ctrl+C, exit 130.
"""
import sys

from nightcore import util
from nightcore.cli import main


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
