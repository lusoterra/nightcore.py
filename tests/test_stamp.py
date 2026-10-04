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
    assert stamp == f"Made with nightcore.py {__version__} ({PROJECT_URL})"
    assert stamp.startswith("Made with nightcore.py ")
    assert __version__ in stamp
    assert PROJECT_URL in stamp


def test_tool_stamp_settings_newline():
    from nightcore.tags import build_pipeline
    pipe = build_pipeline(speed=1.2, tools=[("ffmpeg", "n9.0.2")])
    assert pipe == "NiGhTcOrE speed=1.2x format=flac tools=ffmpeg=n9.0.2"
    assert "Settings:" not in pipe


def test_format_diff_label():
    from nightcore.tags import format_diff_label
    assert format_diff_label() == ""
    assert format_diff_label(1.0, 0.0) == ""
    assert format_diff_label(1.2, 0.0) == "1.2x"
    assert format_diff_label(1.0, 2.16) == "2.16st"
    assert format_diff_label(1.2, 2.16) == "1.2x 2.16st"


def test_version_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["nightcore", "--version"])
    with pytest.raises(SystemExit) as exc:
        parse_arguments()
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_stamp_flag_defaults_off(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["nightcore", "song.flac"])
    assert parse_arguments().no_stamp is False
    assert parse_arguments().no_pipeline is False


def test_no_stamp_flag_opt_in(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["nightcore", "song.flac", "--no-stamp"])
    assert parse_arguments().no_stamp is True
    monkeypatch.setattr(
        sys, "argv", ["nightcore", "song.flac", "--no-pipeline"])
    assert parse_arguments().no_pipeline is True
