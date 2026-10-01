import sys

import pytest

from nightcore import __version__
from nightcore.cli import parse_arguments
from nightcore.constants import PROJECT_URL
from nightcore.tags import tool_stamp


def test_tool_stamp_format():
    stamp = tool_stamp()
    assert stamp == f"Made with nightcore {__version__} ({PROJECT_URL})"
    assert stamp.startswith("Made with nightcore ")
    assert __version__ in stamp
    assert PROJECT_URL in stamp


def test_version_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["nightcore", "--version"])
    with pytest.raises(SystemExit) as exc:
        parse_arguments()
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_stamp_flag_defaults_off(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["nightcore", "song.flac"])
    assert parse_arguments().stamp is False
