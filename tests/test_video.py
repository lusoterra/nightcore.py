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


def test_video_default_from_o_extension(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    out = tmp_path / "friend.mp4"
    rc = _run(monkeypatch,
              [str(src), "-s", "1.1", "-o", str(out),
               "--lossy-opts=-b:a 128k", "-y"])
    assert rc == 0
    kinds = {(s["codec_type"], s["codec_name"])
             for s in _probe(out)["streams"]}
    assert ("video", "h264") in kinds
    assert ("audio", "aac") in kinds


def test_video_unsupported_combo_fails(monkeypatch, tmp_path, capsys):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    out = tmp_path / "out.mp4"
    rc = _run(monkeypatch,
              [str(src), "-s", "1.1", "-F", "av1/flac",
               "-y", "-o", str(out)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "needs a .mkv output, not .mp4" in err


def test_video_qaac_mux_streamcopy(monkeypatch, tmp_path):
    """qaac temp audio is stream-copied, never fed through a filter."""
    _require("ffmpeg", "ffprobe")
    import pathlib, subprocess
    src = tmp_path / "in.mp4"
    _video(src)
    monkeypatch.chdir(tmp_path)
    from nightcore import video as video_mod

    def fake_qaac(wav, out, alac=False, extra_args=()):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(wav),
             "-c:a", "aac", "-b:a", "128k", str(out)],
            check=True)

    original = video_mod.run_qaac_alac
    monkeypatch.setattr(video_mod, "run_qaac_alac", fake_qaac)

    class Args:
        speed = 1.1
        semitones = None
        reciprocal = False
        pitch = False
        resample = False
        denightcore = False
        output = None
        output_folder = None
        overwrite = True
        dry_run = False
        format = "avc/aac"

    ctx = {"tmpl": None, "cover_flag": None, "lossy_opts": ["-V", "91"],
           "backend": {"kind": "qaac"}, "no_stamp": False,
           "no_pipeline": False, "speed": 1.1, "combo": False,
           "st_signed": None, "combo_seq": 1.0, "eff_semitones": None}
    rc = video_mod.run_video_conversion(Args(), src, None, ctx)
    assert rc == 0
    out = tmp_path / "in [1.1x].mp4"
    kinds = {(s["codec_type"], s["codec_name"])
             for s in _probe(out)["streams"]}
    assert ("video", "h264") in kinds
    assert ("audio", "aac") in kinds


def test_video_autorename_uses_template(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    monkeypatch.chdir(tmp_path)
    rc = _run(monkeypatch,
              [str(src), "-s", "1.1", "-F", "mkv", "--autorename", "-y",
               "--lossy-opts=", "-o", str(tmp_path / "x.mp4")])
    # -o overrides autorename; without -o the default template renders
    rc = _run(monkeypatch,
              [str(src), "-s", "1.1", "-F", "mkv", "--autorename", "-y"])
    assert rc == 0
    # Bare --autorename with no artist tag yields the title stem.
    outs = sorted(tmp_path.glob("*.mkv"))
    assert outs, list(tmp_path.iterdir())


def test_video_tag_template_applied(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    tmpl = tmp_path / "pack.txt"
    tmpl.write_text('title="My Mix"\nartist="Video Artist"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = _run(monkeypatch,
              [str(src), "-s", "1.1", "-F", "mkv", "--tag-template",
               str(tmpl), "-y"])
    assert rc == 0
    out = next(p for p in tmp_path.glob("*.mkv"))
    raw = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags",
         "-of", "json", str(out)],
        check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace").stdout
    import json
    fmt = json.loads(raw)
    fmt_tags = {str(k).lower(): v for k, v in
                (fmt.get("format", {}).get("tags") or {}).items()}
    assert fmt_tags.get("title") == "My Mix"
    assert fmt_tags.get("artist") == "Video Artist"


def test_mkvid_still_rejects_speed(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe", "metaflac")
    src = tmp_path / "song.flac"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=2", "-c:a", "flac", str(src)],
        check=True)
    rc = _run(monkeypatch, ["-F", "mkv", "--mkvid", str(src), "-s", "1.1"])
    assert rc == 1
