"""Process state, tool runner, and tiny output helpers."""

import argparse
import atexit
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

def _enable_windows_vt():
    """Best-effort: let Windows consoles render ANSI escapes.

    conhost and Windows Terminal understand ANSI only after
    ENABLE_VIRTUAL_TERMINAL_PROCESSING is set on the console handles.
    Piped handles fail GetConsoleMode and are skipped. Never raises.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            mode = ctypes.c_ulong()
            handle = kernel32.GetStdHandle(handle_id)
            if handle and kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def _help_formatter(*args, **kwargs):
    kwargs.setdefault("max_help_position", 32)
    return argparse.RawTextHelpFormatter(*args, **kwargs)


def missing_tools(*names):
    """Names of required executables not found on PATH."""
    return [n for n in names if shutil.which(n) is None]


# Ctrl+C handling, two stages, no tracebacks either way. Tools run
# detached (own session), so the terminal's SIGINT reaches only this
# process. First press sets _stop_requested: the current file finishes
# and batch loops stop between files (exit 130). Second press kills
# the running tool (so nothing encodes on orphaned) and raises
# SystemExit(130): finally blocks still tidy tempdirs, and the atexit
# hook below deletes tracked partial outputs. Temporary WAVs and
# renametag temps clean themselves via finally.
_active_proc = None


_active_outputs = set()


# Set by the first Ctrl+C; batch loops stop between files when true.
_stop_requested = False


# Set by the second Ctrl+C; the atexit hook reports and cleans up.
_aborted = False


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


def _kill_active_tool():
    """SIGKILL the tracked tool's process group; best effort, never raises.

    Split out so Windows launchers can patch just this (os.killpg
    does not exist on nt) without duplicating the two-stage handler.
    """
    proc = _active_proc
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass


def _on_sigint(signum, frame):
    """Two-stage interrupt: finish-then-stop, then abort.

    First press only flags _stop_requested (batch loops stop between
    files once the current one finishes) and returns, so nothing
    erupts mid-syscall and no traceback prints. Second press kills
    the running tool and raises SystemExit(130): partial outputs stay
    tracked and the atexit hook deletes them.
    """
    global _stop_requested, _aborted
    if not _stop_requested:
        _stop_requested = True
        print("\nInterrupted: finishing the current file, then stopping. "
              "Press Ctrl+C again to abort now "
              "(partial files are deleted).", file=sys.stderr)
        return
    _aborted = True
    _kill_active_tool()
    raise SystemExit(130)


def _cleanup_partials():
    """Delete tracked partial outputs; returns how many went."""
    partials = list(_active_outputs)
    _active_outputs.clear()
    gone = 0
    for partial in partials:
        try:
            Path(partial).unlink(missing_ok=True)
            gone += 1
        except OSError:
            pass
    return gone


def _exit_cleanup():
    """atexit hook: report an abort and delete partial outputs.

    No-op on normal exits (nothing is tracked once files succeed).
    """
    if _aborted:
        gone = _cleanup_partials()
        if gone:
            print(f"Aborted: {gone} partial file(s) deleted.",
                  file=sys.stderr)
        else:
            print("Aborted.", file=sys.stderr)


atexit.register(_exit_cleanup)


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
