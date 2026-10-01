"""End-to-end tests over a real audio file.

These need a FLAC fixture named test.flac in the repo root (any audio
will do; it is gitignored via *.flac, so it never gets committed).
Without it the tests skip with a reminder instead of failing.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nightcore.cli import main
from nightcore.native_tags import native_format_tags
from nightcore.tags import read_format_tag, tool_stamp

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "test.flac"


def _require_fixture():
    if not FIXTURE.is_file():
        pytest.skip(
            "add a FLAC file named test.flac to the repo root "
            "to run end-to-end tests"
        )


def _require_tools(*names):
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        pytest.skip(
            "missing tools for end-to-end tests: " + ", ".join(missing)
        )


def _run(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["nightcore", *argv])
    rc = main()
    out = capsys.readouterr().out
    return rc, out


def _flac_raw_fields(path):
    """Raw VORBIS_COMMENT fields, bypassing ffprobe's key quirks.

    ffprobe reports a stored DESCRIPTION field back as "comment", so
    exact assertions go to metaflac, which shows stored names verbatim.
    """
    out = subprocess.run(
        ["metaflac", "--list", "--block-type=VORBIS_COMMENT", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    fields = {}
    for line in out.splitlines():
        m = re.match(r"\s*comment\[\d+\]:\s*([^=]+)=(.*)$", line)
        if m:
            fields[m.group(1)] = m.group(2)
    return fields


def test_dry_run_predicts_name(monkeypatch, capsys):
    _require_fixture()
    _require_tools("ffprobe")
    rc, out = _run(monkeypatch, capsys, [str(FIXTURE), "-s", "1.2", "-n"])
    assert rc == 0
    assert "[1.2x]" in out


def test_stamp_plain_convert(monkeypatch, capsys, tmp_path):
    _require_fixture()
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    out = tmp_path / "stamped.flac"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(FIXTURE), "-s", "1.2", "--stamp", "-o", str(out)],
    )
    assert rc == 0
    assert out.is_file()
    assert _flac_raw_fields(out).get("DESCRIPTION") == tool_stamp()


def test_stamp_survives_stripping_template(monkeypatch, capsys, tmp_path):
    _require_fixture()
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    tmpl = tmp_path / "strip.tmpl"
    tmpl.write_text('artist="Stamp Artist"\n', encoding="utf-8")
    out = tmp_path / "stripped.flac"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(FIXTURE), "-s", "1.2", "--stamp",
         "--tag-template", str(tmpl), "-o", str(out)],
    )
    assert rc == 0
    # Template applied (everything else stripped)...
    assert read_format_tag(out, "artist") == "Stamp Artist"
    # ...but the explicit stamp rode through the stripping.
    assert _flac_raw_fields(out).get("DESCRIPTION") == tool_stamp()


def _sine_flac(path, seconds=3):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         f"sine=frequency=440:duration={seconds}",
         "-c:a", "flac", str(path)],
        check=True,
    )


def test_opus_stamp_readback_native(monkeypatch, capsys, tmp_path):
    # No fixture needed: a synthesized tone keeps this hermetic.
    # ffprobe cannot parse opus tags at all, so the stamp is read back
    # through opusinfo (the same reader the tool itself uses now).
    _require_tools("ffmpeg", "ffprobe", "opusenc", "opusinfo")
    src = tmp_path / "tone.flac"
    _sine_flac(src)
    out = tmp_path / "tone.opus"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(src), "-s", "1.2", "--stamp", "-F", "opus",
         "--lossy-opts=--bitrate 64", "-o", str(out)],
    )
    assert rc == 0
    fields = native_format_tags(out)
    assert fields is not None
    assert fields.get("description") == tool_stamp()
    assert fields.get("title") == "tone"
