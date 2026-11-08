"""Tests for --mkvid video speedup (real video in, sped h264 out)."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _require(*names):
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        pytest.skip("missing tools: " + ", ".join(missing))


def _video(path):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", str(path)],
        check=True,
    )


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout
    import json
    return json.loads(out)


def _run(monkeypatch, capsys, argv):
    from nightcore.cli import main
    monkeypatch.setattr(sys, "argv", ["nightcore", *argv])
    return main()


def test_mkvid_video_speedup_mp4(monkeypatch, capsys, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    out = tmp_path / "out.mp4"
    rc = _run(monkeypatch, capsys,
              ["-F", "mp4", "--mkvid", str(src), "-s", "1.1",
               "--lossy-opts=-b:a 128k", "-y", "-o", str(out)])
    assert rc == 0
    data = _probe(out)
    assert float(data["format"]["duration"]) == pytest.approx(4 / 1.1, abs=0.2)
    kinds = {(s["codec_type"], s["codec_name"]) for s in data["streams"]}
    assert ("video", "h264") in kinds
    assert ("audio", "aac") in kinds
    vid = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert (vid["width"], vid["height"]) == (320, 240)


def test_mkvid_video_speedup_mkv(monkeypatch, capsys, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    out = tmp_path / "out.mkv"
    rc = _run(monkeypatch, capsys,
              ["-F", "mkv", "--mkvid", str(src), "-s", "1.1",
               "-y", "-o", str(out)])
    assert rc == 0
    kinds = {(s["codec_type"], s["codec_name"])
             for s in _probe(out)["streams"]}
    assert ("video", "h264") in kinds
    assert ("audio", "flac") in kinds


def test_mkvid_video_speedup_mp4alac(monkeypatch, capsys, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    out = tmp_path / "out.mp4"
    rc = _run(monkeypatch, capsys,
              ["-F", "mp4alac", "--mkvid", str(src), "-s", "1.1",
               "-y", "-o", str(out)])
    assert rc == 0
    kinds = {(s["codec_type"], s["codec_name"])
             for s in _probe(out)["streams"]}
    assert ("video", "h264") in kinds
    assert ("audio", "alac") in kinds


def test_mkvid_rejects_speed_for_still_image(monkeypatch, capsys, tmp_path):
    _require("ffmpeg", "ffprobe", "metaflac")
    src = tmp_path / "song.flac"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=2", "-c:a", "flac", str(src)],
        check=True,
    )
    rc = _run(monkeypatch, capsys,
              ["-F", "mkv", "--mkvid", str(src), "-s", "1.1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--mkvid cannot be combined with -s/--speed" in err
