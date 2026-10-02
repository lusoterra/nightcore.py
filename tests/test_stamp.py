import re
import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # python < 3.11 without the tomli backport
    tomllib = None

from nightcore import __version__
from nightcore.cli import parse_arguments
from nightcore.constants import PROJECT_URL
from nightcore.tags import tool_stamp


def _pyproject_version():
    """Expected version, read straight from pyproject.toml (no import)."""
    root = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if tomllib is not None:
        with open(root, "rb") as handle:
            return tomllib.load(handle)["project"]["version"]
    for line in root.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*version\s*=\s*[\"']([^\"']+)[\"']", line)
        if match:
            return match.group(1)
    raise AssertionError("no version found in pyproject.toml")


def test_version_matches_pyproject():
    assert __version__ == _pyproject_version()


def test_tool_stamp_format():
    stamp = tool_stamp()
    assert stamp == f"Made with nightcore {__version__} ({PROJECT_URL})"
    assert stamp.startswith("Made with nightcore ")
    assert __version__ in stamp
    assert PROJECT_URL in stamp


def test_tool_stamp_settings_newline():
    stamp = tool_stamp("Settings: speed=1.2x")
    base, _, settings = stamp.partition("\n")
    assert base == f"Made with nightcore {__version__} ({PROJECT_URL})"
    assert settings == "Settings: speed=1.2x"


def test_version_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["nightcore", "--version"])
    with pytest.raises(SystemExit) as exc:
        parse_arguments()
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_stamp_flag_defaults_off(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["nightcore", "song.flac"])
    assert parse_arguments().no_stamp is False


def test_no_stamp_flag_opt_in(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["nightcore", "song.flac", "--no-stamp"])
    assert parse_arguments().no_stamp is True
