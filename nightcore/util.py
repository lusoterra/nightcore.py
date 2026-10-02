"""Process state, tool runner, and tiny output helpers."""

import argparse
import os
import shutil
import signal
import subprocess
import sys

def _help_formatter(*args, **kwargs):
    kwargs.setdefault("max_help_position", 32)
    return argparse.RawTextHelpFormatter(*args, **kwargs)


def missing_tools(*names):
    """Names of required executables not found on PATH."""
    return [n for n in names if shutil.which(n) is None]


# Ctrl+C handling: any press kills the running tool (if any) and exits
# cleanly at 130 with no traceback. Tools run detached (own session),
# so the terminal's SIGINT reaches only this process; the handler makes
# sure a tracked tool dies too instead of encoding on orphaned.
# Partial outputs tracked in _active_outputs are deleted; tempdirs
# clean themselves via finally.
_active_proc = None


_active_outputs = set()


# Set from -q/--quiet in main(): level 1 hushes tool spam (stages and
# announcements stay), level 2 (-qq) leaves only warnings and errors.
_quiet = 0


def say(msg):
    """Progress chatter for stderr; suppressed by -qq."""
    if _quiet < 2:
        print(msg, file=sys.stderr)


def batch_prefix(ctx):
    """'[i/N] ' file progress in batch mode, else ''.

    Both numbers are zero-padded to the width of the total, so columns
    stay aligned: [001/200], not [1/200].
    """
    total = ctx.get("batch_total")
    if not total:
        return ""
    width = len(str(total))
    return (f"[{ctx.get('batch_index', 0) + 1:0{width}d}"
            f"/{total:0{width}d}] ")


def _on_sigint(signum, frame):
    proc = _active_proc
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    raise KeyboardInterrupt


def run_tool(cmd):
    """Run an audio tool like subprocess.run(cmd, check=True), tracked.

    Detached session (a Ctrl+C handler kill reaches it via its process
    group) and registered in _active_proc for that kill. Raises
    subprocess.CalledProcessError on nonzero exit, same as check=True.
    At quiet level 1+ the tool's output is captured and only shown when
    it fails.
    """
    global _active_proc
    if _quiet >= 1:
        proc = subprocess.Popen(cmd, start_new_session=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    else:
        proc = subprocess.Popen(cmd, start_new_session=True)
    _active_proc = proc
    out = None
    try:
        if _quiet >= 1:
            out, _ = proc.communicate()
            returncode = proc.returncode
        else:
            returncode = proc.wait()
    finally:
        _active_proc = None
    if returncode != 0:
        if _quiet >= 1 and out:
            sys.stderr.buffer.write(out)
            sys.stderr.buffer.flush()
        raise subprocess.CalledProcessError(returncode, cmd)


def preview_output(output_path, title, mode, cover, note="", head=""):
    """One dry-run block: path, predicted title/tags/cover. Stdout."""
    print(f"{head}{output_path}{note}")
    print(f"  title: {title}")
    print(f"  tags: {mode}")
    print(f"  cover: {cover}")


