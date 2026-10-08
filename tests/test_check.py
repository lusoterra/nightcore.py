"""Tests for nightcore --check (dependency report)."""

import sys

from nightcore import backends
from nightcore.cli import main


def _stub_env(monkeypatch, missing=(), broken=()):
    """Stub tool presence: missing names vanish, broken names fail verify."""
    paths = {
        "ffmpeg": "/usr/bin/ffmpeg",
        "ffprobe": "/usr/bin/ffprobe",
        "sox": "/usr/bin/sox",
        "rubberband": "/usr/bin/rubberband",
        "metaflac": "/usr/bin/metaflac",
        "lame": "/usr/bin/lame",
        "opusenc": "/usr/bin/opusenc",
        "opusinfo": "/usr/bin/opusinfo",
        "vorbiscomment": "/usr/bin/vorbiscomment",
        "qaac": "/usr/bin/qaac",
    }
    for name in missing:
        paths.pop(name, None)
    monkeypatch.setattr(backends, "_which",
                        lambda name: paths.get(name))
    monkeypatch.setattr(backends, "_ensure_bundled_on_path",
                        lambda: [])
    monkeypatch.setattr(backends, "tool_version",
                        lambda name: "9.9")
    monkeypatch.setattr(backends, "_mutagen_version",
                        lambda: "1.48.1")
    monkeypatch.setattr(backends, "find_lame",
                        lambda: None if "lame" in missing or "lame" in broken
                        else "lame 4.0")
    monkeypatch.setattr(backends, "find_opusenc",
                        lambda: None if "opusenc" in missing or "opusenc" in broken
                        else "opusenc 0.2")
    monkeypatch.setattr(backends, "find_qaac",
                        lambda: None if "qaac" in missing or "qaac" in broken
                        else "qaac 2.85, CoreAudioToolbox 7.10.9.0")
    monkeypatch.setattr(backends, "coreaudio_toolbox_version",
                        lambda: "7.10.9.0")
    monkeypatch.setattr(backends, "ffmpeg_has_encoder",
                        lambda name: name not in missing)


def test_check_all_present(monkeypatch, capsys):
    _stub_env(monkeypatch)
    assert backends.run_check() == 0
    out = capsys.readouterr().out
    assert "ffmpeg: ok 9.9 (/usr/bin/ffmpeg)" in out
    assert "sox: ok" in out
    assert "mutagen: ok 1.48.1" in out
    assert "qaac: ok 9.9 (/usr/bin/qaac; CoreAudioToolbox 7.10.9.0)" in out
    assert "opusinfo: ok" in out
    assert "ffmpeg libmp3lame: yes" in out
    assert "mp3 encode: ready via lame (ffmpeg libmp3lame fallback ok)" in out
    assert "check: all required tools present" in out


def test_check_missing_required_fails(monkeypatch, capsys):
    _stub_env(monkeypatch, missing=("sox",))
    assert backends.run_check() == 1
    out = capsys.readouterr().out
    assert "sox: MISSING (" in out
    assert "check: MISSING required tools: sox" in out


def test_check_optional_missing_still_ok(monkeypatch, capsys):
    _stub_env(monkeypatch,
              missing=("lame", "opusenc", "qaac", "opusinfo",
                       "vorbiscomment",
                       "libmp3lame", "libopus", "aac", "alac"))
    assert backends.run_check() == 0
    out = capsys.readouterr().out
    assert "lame: MISSING (" in out
    assert "qaac: MISSING (" in out
    assert "(optional)" in out
    assert "mp3 encode: UNAVAILABLE" in out
    assert "opus encode: UNAVAILABLE" in out
    assert "check: all required tools present" in out


def test_check_qaac_broken_reports_dll_hint(monkeypatch, capsys):
    # Binary present but `qaac --check` fails (classic missing-DLLs case).
    _stub_env(monkeypatch, broken=("qaac",))
    assert backends.run_check() == 0
    out = capsys.readouterr().out
    assert "qaac: BROKEN (found at /usr/bin/qaac" in out
    assert "aac encode: ready via ffmpeg aac" in out


def test_check_no_ffmpeg(monkeypatch, capsys):
    _stub_env(monkeypatch, missing=("ffmpeg", "ffprobe",
                                    "libmp3lame", "libopus", "aac",
                                    "alac", "flac"))
    assert backends.run_check() == 1
    out = capsys.readouterr().out
    assert "ffmpeg: MISSING (" in out
    assert "ffmpeg libmp3lame: unknown (no ffmpeg)" in out
    assert "check: MISSING required tools: ffmpeg, ffprobe" in out


def test_cli_check_flag_needs_no_input(monkeypatch, capsys):
    import nightcore.cli as cli
    monkeypatch.setattr(cli, "run_check", lambda: 42)
    monkeypatch.setattr(sys, "argv", ["nightcore", "--check"])
    assert main() == 42


def test_check_live_report_shape(capsys):
    rc = backends.run_check()
    out = capsys.readouterr().out
    assert rc in (0, 1)
    assert out.startswith("nightcore ")
    assert "--check" in out.splitlines()[0]
    assert "python: ok" in out
    assert out.rstrip().splitlines()[-1].startswith("check: ")
