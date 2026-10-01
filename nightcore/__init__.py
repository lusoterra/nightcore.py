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


__version__ = _detect_version()
__all__ = ["__version__"]
