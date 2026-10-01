import pytest

from nightcore.backends import detect_lossy_dialect


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
