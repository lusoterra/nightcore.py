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
    assert "missing Apple DLLs?" in out
    assert "vc_redist" not in out
    assert "aac encode: ready via ffmpeg libfdk_aac" in out


def _stub_redist_env(monkeypatch, probe):
    """Windows full-setup shape: qaac broken, Apple DLLs alongside."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backends, "_which",
                        lambda name: "C:\\tools\\qaac.exe"
                        if name == "qaac" else None)
    monkeypatch.setattr(backends, "find_qaac", lambda: None)
    monkeypatch.setattr(backends, "_apple_dlls_next_to",
                        lambda path: list(backends._APPLE_DLLS))
    monkeypatch.setattr(backends, "_qaac_check_result", probe)


def test_qaac_redist_hint_hedged_without_probe(monkeypatch):
    _stub_redist_env(monkeypatch, None)
    hint = backends.qaac_redist_hint()
    assert "may need vc_redist.x64.exe" in hint


def test_qaac_redist_hint_definitive_on_dll_not_found(monkeypatch):
    import subprocess
    _stub_redist_env(monkeypatch, subprocess.CompletedProcess(
        ["qaac", "--check"], -1073741515, "", ""))
    hint = backends.qaac_redist_hint()
    assert "cannot start" in hint
    assert "vc_redist.x64.exe" in hint


def test_qaac_redist_hint_quiet_off_windows(monkeypatch):
    # Same broken shape on Linux: no redist advice.
    _stub_redist_env(monkeypatch, None)
    monkeypatch.setattr(sys, "platform", "linux")
    assert backends.qaac_redist_hint() == ""


def test_qaac_redist_hint_quiet_without_dlls(monkeypatch):
    _stub_redist_env(monkeypatch, None)
    monkeypatch.setattr(backends, "_apple_dlls_next_to",
                        lambda path: [])
    assert backends.qaac_redist_hint() == ""


def test_check_qaac_broken_with_dlls_hints_redist(monkeypatch, capsys):
    # Full setup/bundle: DLLs installed but the check still fails.
    _stub_env(monkeypatch, broken=("qaac",))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backends, "_apple_dlls_next_to",
                        lambda path: list(backends._APPLE_DLLS))
    monkeypatch.setattr(backends, "_qaac_check_result", None)
    assert backends.run_check() == 0
    out = capsys.readouterr().out
    assert "qaac: BROKEN (found at /usr/bin/qaac" in out
    assert "vc_redist.x64.exe" in out
    assert "missing Apple DLLs?" not in out


def test_apple_dlls_next_to_lists_present(tmp_path):
    (tmp_path / "qaac.exe").write_bytes(b"x")
    (tmp_path / "CoreAudioToolbox.dll").write_bytes(b"x")
    (tmp_path / "ASL.DLL").write_bytes(b"x")
    (tmp_path / "other.dll").write_bytes(b"x")
    found = backends._apple_dlls_next_to(str(tmp_path / "qaac.exe"))
    assert found == ["ASL.dll", "CoreAudioToolbox.dll"]


def test_find_qaac_stashes_probe_result(monkeypatch):
    import subprocess
    probe = subprocess.CompletedProcess(["qaac", "--check"], 1,
                                        "", "DLL load failed")
    monkeypatch.setattr(backends, "_qaac_version", None)
    monkeypatch.setattr(backends, "_qaac_check_result", None)
    monkeypatch.setattr(backends.shutil, "which",
                        lambda name: "C:\\tools\\qaac.exe")
    monkeypatch.setattr(backends.subprocess, "run",
                        lambda *a, **k: probe)
    assert backends.find_qaac() is None
    assert backends._qaac_check_result is probe


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
