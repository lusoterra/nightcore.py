import shutil
import subprocess
import sys

import pytest

from nightcore import backends
from nightcore.backends import (
    coreaudio_toolbox_version,
    detect_lossy_dialect,
    mp3_postprocess,
)


def test_detect_cli_dialect():
    assert detect_lossy_dialect("mp3", ["-V", "0"]) == "cli"
    assert detect_lossy_dialect("opus", ["--bitrate", "128"]) == "cli"
    assert detect_lossy_dialect("aac", ["-V", "91"]) == "cli"


def test_detect_ffmpeg_dialect():
    assert detect_lossy_dialect("mp3", ["-q:a", "0"]) == "ffmpeg"
    assert detect_lossy_dialect("opus", ["-b:a", "128k"]) == "ffmpeg"


def test_detect_none():
    assert detect_lossy_dialect("mp3", []) is None


def test_detect_mixed_raises():
    with pytest.raises(ValueError):
        detect_lossy_dialect("mp3", ["-V", "0", "-q:a", "0"])


def test_coreaudio_toolbox_version(monkeypatch):
    monkeypatch.setattr(
        backends, "find_qaac",
        lambda: "qaac 2.85, CoreAudioToolbox 7.10.9.0")
    assert coreaudio_toolbox_version() == "7.10.9.0"


def test_coreaudio_toolbox_version_missing(monkeypatch):
    monkeypatch.setattr(backends, "find_qaac", lambda: None)
    assert coreaudio_toolbox_version() is None
    monkeypatch.setattr(backends, "find_qaac", lambda: False)
    assert coreaudio_toolbox_version() is None
    monkeypatch.setattr(backends, "find_qaac", lambda: "qaac 2.85")
    assert coreaudio_toolbox_version() is None


def _sine_mp3(path):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=1", "-c:a", "libmp3lame",
         "-metadata", "compilation=1", str(path)],
        check=True, capture_output=True)


def test_mp3_postprocess_tcmp(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not found")
    mutagen_mp3 = pytest.importorskip("mutagen.mp3")
    src = tmp_path / "tone.mp3"
    try:
        _sine_mp3(src)
    except subprocess.CalledProcessError:
        pytest.skip("libmp3lame unavailable")
    assert mp3_postprocess(src, "1") is True
    track = mutagen_mp3.MP3(str(src))
    assert track.get("TCMP") is not None
    dupes = [k for k in track.keys()
             if k.startswith("TXXX:")
             and k.split(":", 1)[1].upper()
             in ("TCMP", "COMPILATION", "ITUNESCOMPILATION")]
    assert dupes == []


def test_mp3_backend_prefers_lame(monkeypatch):
    from nightcore import backends
    monkeypatch.setattr(backends, "find_lame", lambda: "lame 4.0")
    monkeypatch.setattr(backends, "ffmpeg_has_encoder",
                        lambda name: True)
    assert backends.select_lossy_backend("mp3", None) == {"kind": "lame"}


def test_mp3_backend_falls_back_to_libmp3lame(monkeypatch):
    from nightcore import backends
    monkeypatch.setattr(backends, "find_lame", lambda: None)
    monkeypatch.setattr(backends, "ffmpeg_has_encoder",
                        lambda name: True)
    assert backends.select_lossy_backend("mp3", None) == {"kind": "ffmpeg"}


def test_mp3_backend_none_available(monkeypatch):
    from nightcore import backends
    monkeypatch.setattr(backends, "find_lame", lambda: None)
    monkeypatch.setattr(backends, "ffmpeg_has_encoder",
                        lambda name: False)
    assert backends.select_lossy_backend("mp3", None) is None


def test_mp3_forced_dialect_kept(monkeypatch):
    from nightcore import backends
    monkeypatch.setattr(backends, "find_lame", lambda: "lame 4.0")
    monkeypatch.setattr(backends, "ffmpeg_has_encoder",
                        lambda name: True)
    assert backends.select_lossy_backend("mp3", "ffmpeg") == {
        "kind": "ffmpeg"}
    assert backends.select_lossy_backend("mp3", "cli") == {"kind": "lame"}


def test_mp3_postprocess_no_mutagen(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "mutagen.mp3", None)
    assert mp3_postprocess(tmp_path / "x.mp3", "1") is False


def test_aac_backend_prefers_qaac(monkeypatch):
    from nightcore import backends
    monkeypatch.setattr(backends, "find_qaac", lambda: "qaac 2.85")
    monkeypatch.setattr(backends, "ffmpeg_has_encoder", lambda name: True)
    assert backends.select_lossy_backend("aac", None) == {"kind": "qaac"}


def test_aac_backend_falls_back_to_fdk(monkeypatch):
    from nightcore import backends
    monkeypatch.setattr(backends, "find_qaac", lambda: None)
    monkeypatch.setattr(backends, "ffmpeg_has_encoder",
                        lambda name: name == "libfdk_aac")
    assert backends.select_lossy_backend("aac", None) == {
        "kind": "ffmpeg", "encoder": "libfdk_aac"}


def test_aac_backend_falls_back_to_native(monkeypatch):
    from nightcore import backends
    monkeypatch.setattr(backends, "find_qaac", lambda: None)
    monkeypatch.setattr(backends, "ffmpeg_has_encoder",
                        lambda name: name == "aac")
    assert backends.select_lossy_backend("aac", None) == {
        "kind": "ffmpeg", "encoder": "aac"}


def test_aac_backend_none_available(monkeypatch):
    from nightcore import backends
    monkeypatch.setattr(backends, "find_qaac", lambda: None)
    monkeypatch.setattr(backends, "ffmpeg_has_encoder", lambda name: False)
    assert backends.select_lossy_backend("aac", None) is None
