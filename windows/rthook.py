# PyInstaller runtime hook for nightcore single-file bundle.
# Runs before nightcore imports: puts bundled tools on PATH/DLL search,
# then patches the one POSIX-only call (os.killpg in SIGINT handler).
import os
import sys


def _candidate_dirs():
    dirs = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(os.path.join(meipass, "tools"))
        dirs.append(meipass)
    exe = sys.executable if getattr(sys, "frozen", False) else None
    if exe:
        exedir = os.path.dirname(os.path.abspath(exe))
        dirs.append(os.path.join(exedir, "tools"))
        dirs.append(exedir)
    # dev fallback: <repo>/tools next to this hook source
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        dirs.append(os.path.join(here, "tools"))
    except Exception:
        pass
    seen = []
    for d in dirs:
        if d and os.path.isdir(d) and d not in seen:
            seen.append(d)
    return seen


_DIRS = _candidate_dirs()
if _DIRS:
    sep = os.pathsep
    os.environ["PATH"] = sep.join(_DIRS) + sep + os.environ.get("PATH", "")
    if os.name == "nt":
        try:
            for d in _DIRS:
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass
        except AttributeError:
            pass

# Windows-safe Ctrl+C: os.killpg does not exist on nt. Patch after import;
# hook runs early so the kill helper the SIGINT handler calls is fixed
# before anything runs (patching util._kill_active_tool is sufficient).
try:
    import nightcore.util as _u

    if os.name == "nt":
        def _kill_active_tool_win():
            proc = getattr(_u, "_active_proc", None)
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass

        _u._kill_active_tool = _kill_active_tool_win
except Exception:
    pass
