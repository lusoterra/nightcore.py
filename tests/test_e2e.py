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
    last_key = None
    for line in out.splitlines():
        m = re.match(r"\s*comment\[\d+\]:\s*([^=]+)=(.*)$", line)
        if m:
            last_key = m.group(1)
            fields[last_key] = m.group(2)
        elif (last_key is not None and line
                and not line.startswith((" ", "METADATA"))):
            # Multiline values (e.g. the stamp's Settings second line)
            # print as bare continuation lines: fold them back.
            fields[last_key] += "\n" + line.strip()
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
        [str(FIXTURE), "-s", "1.2", "-o", str(out)],
    )
    assert rc == 0
    assert out.is_file()
    stamped = _flac_raw_fields(out).get("DESCRIPTION")
    assert stamped is not None
    assert stamped.startswith(tool_stamp() + "\n")
    assert "speed=1.2x" in stamped


def test_no_stamp_disables(monkeypatch, capsys, tmp_path):
    _require_fixture()
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    out = tmp_path / "unstamped.flac"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(FIXTURE), "-s", "1.2", "--no-stamp", "-o", str(out)],
    )
    assert rc == 0
    stamped = _flac_raw_fields(out).get("DESCRIPTION") or ""
    assert not stamped.startswith("Made with nightcore ")


def test_default_title_is_diff_only(monkeypatch, capsys, tmp_path):
    _require_fixture()
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    out = tmp_path / "titled.flac"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(FIXTURE), "-s", "1.2", "-o", str(out)],
    )
    assert rc == 0
    title = read_format_tag(out, "title")
    assert title is not None
    assert "[Nightcore 1.2x]" in title
    assert "16bit" not in title and "kHz" not in title.replace(
        "[Nightcore 1.2x]", "")


def test_template_title_slots_full_provenance(monkeypatch, capsys, tmp_path):
    _require_fixture()
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    tmpl = tmp_path / "slots.tmpl"
    tmpl.write_text('title="{title} [{diff}] [{quality}]"\n',
                    encoding="utf-8")
    out = tmp_path / "full.flac"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(FIXTURE), "-s", "1.2",
         "--tag-template", str(tmpl), "-o", str(out)],
    )
    assert rc == 0
    title = read_format_tag(out, "title")
    assert title is not None
    assert "[1.2x]" in title
    assert "bit" in title or "kHz" in title


def test_stamp_survives_stripping_template(monkeypatch, capsys, tmp_path):
    _require_fixture()
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    tmpl = tmp_path / "strip.tmpl"
    tmpl.write_text('artist="Stamp Artist"\n', encoding="utf-8")
    out = tmp_path / "stripped.flac"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(FIXTURE), "-s", "1.2",
         "--tag-template", str(tmpl), "-o", str(out)],
    )
    assert rc == 0
    # Template applied (everything else stripped)...
    assert read_format_tag(out, "artist") == "Stamp Artist"
    # ...but the stamp rode through the stripping.
    stamped = _flac_raw_fields(out).get("DESCRIPTION")
    assert stamped is not None
    assert stamped.startswith(tool_stamp() + "\n")


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
        [str(src), "-s", "1.2", "-F", "opus",
         "--lossy-opts=--bitrate 64", "-o", str(out)],
    )
    assert rc == 0
    fields = native_format_tags(out)
    assert fields is not None
    assert fields.get("description", "").startswith(tool_stamp())
    assert fields.get("title") == "tone [Nightcore 1.2x]"


def test_renametag_backfills_stamp(monkeypatch, capsys, tmp_path):
    # Old library file: filename/title tags but a stamp without Settings.
    # -N rebuilds the stamp from the filename so -D works afterwards.
    _require_fixture()
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    work = tmp_path / "work.flac"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(FIXTURE), "-s", "1.2", "--no-stamp", "-o", str(work)],
    )
    assert rc == 0
    tagged = tmp_path / "old nightcore [1.2x].flac"
    work.rename(tagged)
    fixed = tmp_path / "fixed.flac"
    rc, _ = _run(monkeypatch, capsys, [str(tagged), "-N", "-o", str(fixed)])
    assert rc == 0
    stamped = _flac_raw_fields(fixed).get("DESCRIPTION")
    assert stamped is not None and "speed=1.2x" in stamped
    title = read_format_tag(fixed, "title")
    assert title is not None and "[1.2x]" not in title


def test_denightcore_stamp_only_roundtrip(monkeypatch, capsys, tmp_path):
    # -D trusts the stamp, not the filename: the nightcored file is
    # renamed to a tagless name first, then restored.
    _require_fixture()
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    work = tmp_path / "work.flac"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(FIXTURE), "-s", "1.2", "-o", str(work)],
    )
    assert rc == 0
    renamed = tmp_path / "totally renamed.flac"
    work.rename(renamed)
    rc, _ = _run(monkeypatch, capsys, [str(renamed), "-D", "-y"])
    assert rc == 0
    outs = sorted(tmp_path.glob("*denightcored BP*.flac"))
    assert len(outs) == 1
    title = read_format_tag(outs[0], "title")
    assert title is not None and title.endswith("[denightcored BP]")
    assert "[1.2x]" not in title and "Nightcore" not in title


def test_denightcore_needs_stamp(monkeypatch, capsys, tmp_path):
    # An unstamped file with shift-looking filename tags is refused:
    # filenames alone are not trusted.
    _require_tools("ffmpeg", "ffprobe")
    src = tmp_path / "tone.flac"
    _sine_flac(src)
    tagged = tmp_path / "tone [1.2x].flac"
    src.rename(tagged)
    rc, _ = _run(
        monkeypatch, capsys,
        [str(tagged), "-D", "--no-stamp", "-o", str(tmp_path / "out.flac")],
    )
    assert rc == 1
