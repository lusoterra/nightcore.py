import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nightcore import native_tags
from nightcore.audio import probe_audio
from nightcore.native_tags import native_format_tags

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "test.flac"

METAFLAC_SAMPLE = """\
METADATA block #1
  type: 4 (VORBIS_COMMENT)
  is last: false
  length: 60
  vendor string: reference libFLAC 1.5.0
  comments: 3
    comment[0]: ARTIST=Code Red
    comment[1]: TITLE=A=B
    comment[2]: DESCRIPTION=line1
line2
"""

OPUSINFO_SAMPLE = """\
Processing file "m.opus"...

New logical stream (#1, serial: 22f9c0f0): type opus
Encoded with libopus 1.6.1, libopusenc 0.3
User comments section follows...
\tENCODER=opusenc from opus-tools 0.2
\ttitle=Some Title
\tdescription=Made with nightcore 2026.10.01-1 (https://example.invalid/nightcore)
Opus stream 1:
\tPre-skip: 312
"""

VORBISCOMMENT_SAMPLE = "ARTIST=Code Red\nTITLE=Ogg Title\ndescription=Ogg Desc\n"


def _fake_run(stdout, returncode=0):
    def fake(cmd):
        return SimpleNamespace(returncode=returncode, stdout=stdout)
    return fake


def _parse(monkeypatch, path, stdout, returncode=0):
    monkeypatch.setattr(native_tags, "_run", _fake_run(stdout, returncode))
    native_tags._native_cache.clear()
    return native_format_tags(path)


def test_metaflac_parser(monkeypatch, tmp_path):
    fields = _parse(monkeypatch, tmp_path / "x.flac", METAFLAC_SAMPLE)
    assert fields == {
        "ARTIST": "Code Red",
        "TITLE": "A=B",
        "DESCRIPTION": "line1\nline2",
    }


def test_metaflac_failure_returns_none(monkeypatch, tmp_path):
    assert _parse(monkeypatch, tmp_path / "x.flac", "", returncode=1) is None


def test_metaflac_missing_binary_returns_none(monkeypatch, tmp_path):
    def missing(cmd):
        raise FileNotFoundError("metaflac")
    monkeypatch.setattr(native_tags, "_run", missing)
    native_tags._native_cache.clear()
    assert native_format_tags(tmp_path / "x.flac") is None


def test_opusinfo_parser(monkeypatch, tmp_path):
    fields = _parse(monkeypatch, tmp_path / "x.opus", OPUSINFO_SAMPLE)
    assert fields["ENCODER"] == "opusenc from opus-tools 0.2"
    assert fields["title"] == "Some Title"
    assert fields["description"].startswith("Made with nightcore ")


def test_opusinfo_without_marker_returns_none(monkeypatch, tmp_path):
    fields = _parse(monkeypatch, tmp_path / "x.opus",
                    "Processing file \"x.opus\"...\nWARNING: garbage\n")
    assert fields is None


def test_vorbiscomment_parser(monkeypatch, tmp_path):
    fields = _parse(monkeypatch, tmp_path / "x.ogg", VORBISCOMMENT_SAMPLE)
    assert fields == {
        "ARTIST": "Code Red",
        "TITLE": "Ogg Title",
        "description": "Ogg Desc",
    }


def test_no_reader_for_other_extensions(tmp_path):
    native_tags._native_cache.clear()
    assert native_format_tags(tmp_path / "x.mp3") is None
    assert native_format_tags(tmp_path / "x.m4a") is None
    assert native_format_tags(tmp_path / "x.wav") is None


def test_results_are_cached(monkeypatch, tmp_path):
    calls = []

    def counting(cmd):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="TITLE=X\n")

    monkeypatch.setattr(native_tags, "_run", counting)
    native_tags._native_cache.clear()
    target = tmp_path / "x.ogg"
    assert native_format_tags(target) == {"TITLE": "X"}
    assert native_format_tags(target) == {"TITLE": "X"}
    assert len(calls) == 1


def _require_tools(*names):
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        pytest.skip("missing tools for native-tag tests: " + ", ".join(missing))


def test_flac_parity_with_ffprobe():
    if not FIXTURE.is_file():
        pytest.skip("add a FLAC file named test.flac to the repo root")
    _require_tools("ffprobe", "metaflac")
    native_tags._native_cache.clear()
    native = {k.lower(): v for k, v in native_format_tags(FIXTURE).items()}
    probed = {k.lower(): v for k, v in probe_audio(FIXTURE)["format_tags"].items()}
    for key in ("title", "artist", "album", "genre", "date"):
        assert native.get(key) == probed.get(key)
        assert native.get(key) is not None
