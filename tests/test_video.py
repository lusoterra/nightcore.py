"""Tests for video speedup via -F presets and slash pairs."""
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


def _run(monkeypatch, argv):
    from nightcore.cli import main
    monkeypatch.setattr(sys, "argv", ["nightcore", *argv])
    return main()


def test_video_preset_mp4(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    out = tmp_path / "out.mp4"
    rc = _run(monkeypatch,
              [str(src), "-s", "1.1", "-F", "mp4",
               "--lossy-opts=-b:a 128k", "-y", "-o", str(out)])
    assert rc == 0
    data = _probe(out)
    assert float(data["format"]["duration"]) == pytest.approx(4 / 1.1, abs=0.2)
    kinds = {(s["codec_type"], s["codec_name"]) for s in data["streams"]}
    assert ("video", "h264") in kinds
    assert ("audio", "aac") in kinds
    vid = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert (vid["width"], vid["height"]) == (320, 240)


def test_video_pair_reversible(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    out = tmp_path / "out.mp4"
    # aac/h264 reversed order must also guess mp4.
    rc = _run(monkeypatch,
              [str(src), "-s", "1.1", "-F", "aac/h264",
               "--lossy-opts=-b:a 128k", "-y", "-o", str(out)])
    assert rc == 0
    kinds = {(s["codec_type"], s["codec_name"])
             for s in _probe(out)["streams"]}
    assert ("video", "h264") in kinds
    assert ("audio", "aac") in kinds


def test_video_preset_mkv(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    out = tmp_path / "out.mkv"
    rc = _run(monkeypatch,
              [str(src), "-s", "1.1", "-F", "mkv", "-y", "-o", str(out)])
    assert rc == 0
    kinds = {(s["codec_type"], s["codec_name"])
             for s in _probe(out)["streams"]}
    assert ("video", "h264") in kinds
    assert ("audio", "flac") in kinds


def test_ass_sidecar_retimed(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    ass = tmp_path / "in.ass"
    ass.write_text(
        "[Script Info]\nTitle: t\n\n[V4+ Styles]\nFormat: Name\nStyle: Default\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:01:00.00,0:01:03.00,Default,,0,0,0,,Hello\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = _run(monkeypatch, [
        str(src), str(ass), "-s", "1.1", "-F", "mp4",
        "--lossy-opts=-b:a 128k", "-y"])
    assert rc == 0
    retimed = tmp_path / "in [1.1x].ass"
    assert retimed.is_file()
    assert "Dialogue:" in retimed.read_text(encoding="utf-8")
    # 60.00s / 1.1 = 54.54s
    assert "0:00:54.54" in retimed.read_text(encoding="utf-8")


def test_video_av1_flac_pair(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe")
    from nightcore.backends import ffmpeg_has_encoder
    if not ffmpeg_has_encoder("libsvtav1"):
        pytest.skip("libsvtav1 unavailable")
    src = tmp_path / "in.mp4"
    _video(src)
    out = tmp_path / "out.mkv"
    rc = _run(monkeypatch,
              [str(src), "-s", "1.1", "-F", "av1/flac", "-y", "-o", str(out)])
    assert rc == 0
    kinds = {(s["codec_type"], s["codec_name"])
             for s in _probe(out)["streams"]}
    assert ("video", "av1") in kinds
    assert ("audio", "flac") in kinds


def test_mkvid_still_rejects_speed(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe", "metaflac")
    src = tmp_path / "song.flac"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=2", "-c:a", "flac", str(src)],
        check=True)
    rc = _run(monkeypatch, ["-F", "mkv", "--mkvid", str(src), "-s", "1.1"])
    assert rc == 1
