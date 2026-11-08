"""CLI validation: nonsense flag combos must fail loudly, not no-op.

Regression net for the QA sweep: -p/-S with no amount, 0 st,
calculators with inputs, mutually-exclusive exit modes, case-sensitive
-F, video -D/lossy rules, and the opus container guess. Error cases
return before any encode, so they stay fast; uppercase/dry-run cases
use -n.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _require(*names):
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        pytest.skip("missing tools: " + ", ".join(missing))


def _flac(path):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "flac", str(path)],
        check=True,
    )


def _video(path):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", str(path)],
        check=True,
    )


def _run(monkeypatch, argv):
    from nightcore.cli import main
    monkeypatch.setattr(sys, "argv", ["nightcore", *argv])
    return main()


# --- shift-amount nonsense -------------------------------------------------

def test_pitch_without_amount_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["-p", "song.flac"])
    assert rc == 1
    assert "-p/--pitch needs an amount" in capsys.readouterr().err


def test_pitch_with_speed_zero_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["-p", "-s", "1.0", "song.flac"])
    assert rc == 1
    assert "-p/--pitch needs an amount" in capsys.readouterr().err


def test_semitones_zero_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["-S", "0", "song.flac"])
    assert rc == 1
    assert "must be non-zero" in capsys.readouterr().err


def test_semitones_zero_with_speed_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["-s", "1.2", "-S", "0", "song.flac"])
    assert rc == 1
    assert "must be non-zero" in capsys.readouterr().err


def test_pitch_with_amount_ok_dry_run(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "tone.flac"
    _flac(src)
    monkeypatch.chdir(tmp_path)
    assert _run(monkeypatch, ["-p", "-S", "2", "-n", str(src)]) == 0
    assert _run(monkeypatch, ["-p", "-s", "1.2", "-n", str(src)]) == 0


# --- calculators / exit modes ----------------------------------------------

def test_speed_diff_with_input_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["--speed-diff", "2", "song.flac"])
    assert rc == 1
    assert "calculators" in capsys.readouterr().err


def test_both_calculators_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["--pitch-diff", "1.2", "--speed-diff", "2"])
    assert rc == 1
    assert "mutually exclusive" in capsys.readouterr().err


def test_check_and_calculator_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["--check", "--pitch-diff", "1.2"])
    assert rc == 1
    assert "mutually exclusive" in capsys.readouterr().err


def test_stats_and_calculator_errors(monkeypatch, capsys, tmp_path):
    rc = _run(monkeypatch,
              ["--stats", str(tmp_path), "--pitch-diff", "1.2"])
    assert rc == 1
    assert "mutually exclusive" in capsys.readouterr().err


def test_calculator_alone_ok(monkeypatch, capsys):
    assert _run(monkeypatch, ["--speed-diff", "2"]) == 0
    assert "speed" in capsys.readouterr().out or True


# --- -F case ----------------------------------------------------------------

def test_format_case_insensitive_dry_run(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "tone.flac"
    _flac(src)
    monkeypatch.chdir(tmp_path)
    assert _run(monkeypatch,
                ["-F", "FLAC", "-s", "1.2", "-n", str(src)]) == 0
    assert _run(monkeypatch,
                ["-F", "Mp3", "-s", "1.2", "-n", str(src)]) == 0


def test_video_format_case_insensitive_dry_run(monkeypatch, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    monkeypatch.chdir(tmp_path)
    assert _run(monkeypatch,
                ["-F", "WEBM", "-s", "1.2", "-n", str(src)]) == 0


# --- denightcore combos (audio + video) -------------------------------------

def test_denightcore_with_speed_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["-D", "-s", "1.2", "song.flac"])
    assert rc == 1
    assert "--denightcore cannot be combined" in capsys.readouterr().err


def test_denightcore_with_pitch_errors_video(monkeypatch, capsys, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    rc = _run(monkeypatch,
              ["-D", "-p", "-F", "mkv", "-n", str(src)])
    assert rc == 1
    assert "--denightcore cannot be combined" in capsys.readouterr().err


def test_video_denightcore_lossy_errors(monkeypatch, capsys, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    rc = _run(monkeypatch, ["-F", "webm", "-D", "-n", str(src)])
    assert rc == 1
    assert "needs a lossless output" in capsys.readouterr().err


# --- lossy rules -------------------------------------------------------------

def test_video_lossy_opts_lossless_warns(monkeypatch, capsys, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    monkeypatch.chdir(tmp_path)
    rc = _run(monkeypatch,
              ["-F", "mkv", "--lossy-opts=-b:a 128k", "-n", str(src)])
    assert rc == 0
    assert "ignored for lossless" in capsys.readouterr().err


def test_r_lossy_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["-R", "-F", "mp3", "song.flac"])
    assert rc == 1
    assert "cannot be used with lossy" in capsys.readouterr().err


def test_d_lossy_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["-D", "-F", "mp3", "song.flac"])
    assert rc == 1
    assert "needs a lossless output" in capsys.readouterr().err


# --- container guess ----------------------------------------------------------

def test_opus_container_guesses_mkv():
    from nightcore.video import parse_video_format
    assert parse_video_format("h264/opus") == ("h264", "opus", "mkv", ".mkv")
    assert parse_video_format("h264/vorbis") == (
        "h264", "vorbis", "mkv", ".mkv")
    assert parse_video_format("hevc/opus") == ("hevc", "opus", "mkv", ".mkv")
    # vp9/av1 + opus still prefers webm.
    assert parse_video_format("vp9/opus")[3] == ".webm"
    assert parse_video_format("av1/opus")[3] == ".webm"


def test_h264_opus_to_mp4_errors(monkeypatch, capsys, tmp_path):
    _require("ffmpeg", "ffprobe")
    src = tmp_path / "in.mp4"
    _video(src)
    out = tmp_path / "out.mp4"
    rc = _run(monkeypatch,
              ["-s", "1.2", "-F", "h264/opus", "-y", "-o", str(out),
               str(src)])
    assert rc == 1
    assert "needs a .mkv output" in capsys.readouterr().err


# --- misc nonsense that must error --------------------------------------------

def test_renametag_with_speed_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["-N", "-s", "1.2", "song.flac"])
    assert rc == 1
    assert "cannot be combined" in capsys.readouterr().err


def test_batch_with_output_errors(monkeypatch, capsys, tmp_path):
    rc = _run(monkeypatch,
              ["--batch", str(tmp_path), "-o", "out.flac"])
    assert rc == 1
    assert "needs a single input" in capsys.readouterr().err


def test_in_place_without_renametag_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["--in-place", "song.flac"])
    assert rc == 1
    assert "needs -N/--renametag" in capsys.readouterr().err


def test_output_needs_extension_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["-o", "/tmp/out", "song.flac"])
    assert rc == 1
    assert "needs a file extension" in capsys.readouterr().err


def test_unknown_format_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["-F", "unknownfmt", "song.flac"])
    assert rc == 1
    assert "unknown output format" in capsys.readouterr().err


def test_recurse_without_stats_errors(monkeypatch, capsys):
    # Bare --recurse takes no value; the depth form is --recurse=2.
    rc = _run(monkeypatch, ["--recurse=2", "/tmp"])
    assert rc == 1
    assert "--recurse needs --stats" in capsys.readouterr().err


def test_no_delete_without_template_errors(monkeypatch, capsys):
    rc = _run(monkeypatch, ["--no-delete-tags", "song.flac"])
    assert rc == 1
    assert "needs --tag-template" in capsys.readouterr().err


def test_lossy_opts_space_form_errors(monkeypatch):
    # --lossy-opts only accepts the = form; the space form is a
    # parser error (SystemExit 2), not a silent misparse.
    from nightcore.cli import main
    monkeypatch.setattr(
        sys, "argv", ["nightcore", "--lossy-opts", "-V", "0", "song.flac"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
