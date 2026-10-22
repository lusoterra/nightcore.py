"""nightcore: bit-perfect nightcore/speed/pitch audio tool."""

from __future__ import annotations

import re
from pathlib import Path


def _version_from_pyproject() -> str | None:
    """Read ``version`` from the checkout's ``pyproject.toml``, if present.

    Dependency-free (regex, not tomllib) so this works on every
    supported python (>=3.10) with no extra backports. Only the
    ``[project]`` table's version is considered.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            return None
        in_project = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_project = stripped == "[project]"
                continue
            if in_project:
                match = re.match(r"version\s*=\s*[\"']([^\"']+)[\"']", stripped)
                if match:
                    return match.group(1)
        return None
    return None


def _detect_version() -> str:
    """Single source of truth: pyproject.toml in a checkout, else the
    installed dist metadata, else an explicit unknown marker."""
    from_pyproject = _version_from_pyproject()
    if from_pyproject:
        return from_pyproject
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("nightcore")
    except Exception:  # not installed / no metadata available
        return "0.0.0+unknown"


def _git_hash_from_baked_file() -> str | None:
    """Build-time hash, when a packager baked one in (see below).

    A ``_git_hash.py`` sibling with ``GIT_HASH = "<hex>"`` lets wheels
    or sdists built from a git checkout carry the commit without
    needing git (or pip's direct_url.json) at runtime. Absent by
    default; never raises.
    """
    try:
        from ._git_hash import GIT_HASH as baked
    except Exception:
        return None
    if isinstance(baked, str) and baked.strip():
        return baked.strip()
    return None


def _git_hash_from_direct_url() -> str | None:
    """Commit hash recorded by pip for ``pip install git+...`` (PEP 610).

    pip writes ``direct_url.json`` into the dist metadata with the
    resolved ``commit_id`` when installing from a VCS URL, which
    survives into the installed environment even though there is no
    ``.git`` directory next to the code. Returns the short (7-char)
    hash, or None.
    """
    try:
        import json
        from importlib.metadata import distribution
    except Exception:
        return None
    try:
        dist = distribution("nightcore")
        text = dist.read_text("direct_url.json")
    except Exception:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    try:
        vcs_info = data.get("vcs_info") or {}
        if vcs_info.get("vcs") != "git":
            return None
        commit = vcs_info.get("commit_id")
    except AttributeError:
        return None
    if isinstance(commit, str) and commit.strip():
        return commit.strip()[:7]
    return None


def _git_hash_from_checkout() -> str | None:
    """Live ``git`` hash when running from a checkout, else None.

    Appends ``.dirty`` when tracked files differ from HEAD (untracked
    files ignored, mirroring release.sh), so dev builds never masquerade
    as clean. Best-effort: any failure (no git, no repo, timeout)
    returns None. Never raises.

    Only trusts a repo that actually owns this file: an installed copy
    sitting inside some unrelated git checkout (e.g. a PKGBUILD
    ``pkg/`` dir) must not report that repo's hash as nightcore's.
    """
    import subprocess

    here = Path(__file__).resolve()
    cwd = here.parent
    try:
        top_proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    if top_proc.returncode != 0:
        return None
    top_text = (top_proc.stdout or "").strip()
    if not top_text:
        return None
    try:
        toplevel = Path(top_text).resolve()
        here.relative_to(toplevel)
        owned = (toplevel / "nightcore" / "__init__.py").resolve()
    except Exception:
        return None
    if owned != here:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    short = (proc.stdout or "").strip()
    if not short:
        return None
    try:
        dirty_work = subprocess.run(
            ["git", "-C", str(cwd), "diff", "--quiet"],
            capture_output=True, timeout=2,
        )
        dirty_index = subprocess.run(
            ["git", "-C", str(cwd), "diff", "--cached", "--quiet"],
            capture_output=True, timeout=2,
        )
        if dirty_work.returncode != 0 or dirty_index.returncode != 0:
            short += ".dirty"
    except Exception:
        pass
    return short


def _detect_git_hash() -> str | None:
    """Short git commit hash, or None when not a git install/checkout."""
    for source in (_git_hash_from_baked_file,
                   _git_hash_from_direct_url,
                   _git_hash_from_checkout):
        try:
            found = source()
        except Exception:
            continue
        if found:
            return found
    return None


__version__ = _detect_version()
__git_hash__ = _detect_git_hash()
if __git_hash__:
    __full_version__ = f"{__version__}+g{__git_hash__}"
else:
    __full_version__ = __version__
__all__ = ["__version__", "__git_hash__", "__full_version__"]
