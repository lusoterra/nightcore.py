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
    assert "[Nightcore 1.2x]" in out


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
    fields = _flac_raw_fields(out)
    assert fields.get("DESCRIPTION") == tool_stamp()
    pipe = fields.get("encoder_settings") or ""
    assert "speed=1.2x" in pipe
    assert "tools=ffmpeg=" in pipe
    assert "Settings:" not in pipe


def test_no_stamp_disables(monkeypatch, capsys, tmp_path):
    _require_fixture()
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    out = tmp_path / "unstamped.flac"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(FIXTURE), "-s", "1.2", "--no-stamp", "-o", str(out)],
    )
    assert rc == 0
    fields = _flac_raw_fields(out)
    assert not (fields.get("DESCRIPTION") or "").startswith(
        "Made with nightcore")
    # The pipeline is independent: still written.
    assert "speed=1.2x" in (fields.get("encoder_settings") or "")


def test_no_pipeline_disables(monkeypatch, capsys, tmp_path):
    _require_fixture()
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    out = tmp_path / "nopipe.flac"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(FIXTURE), "-s", "1.2", "--no-pipeline", "-o", str(out)],
    )
    assert rc == 0
    fields = _flac_raw_fields(out)
    assert fields.get("DESCRIPTION") == tool_stamp()
    assert not any(k.lower() == "encoder_settings" for k in fields)


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
    # ...but both marks rode through the stripping.
    fields = _flac_raw_fields(out)
    assert fields.get("DESCRIPTION") == tool_stamp()
    assert "speed=1.2x" in (fields.get("encoder_settings") or "")


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
    assert "speed=1.2x" in fields.get("encoder_settings", "")
    assert fields.get("title") == "tone [Nightcore 1.2x]"


def test_lossy_source_pipeline_fingerprint(monkeypatch, capsys, tmp_path):
    # No fixture needed: synthesized tones keep this hermetic. Lossy
    # sources (AAC here) record source-mode/source-bitrate in the
    # pipeline; lossless sources record depth instead, never bitrate.
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    src = tmp_path / "tone.m4a"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=3",
         "-c:a", "aac", "-b:a", "128k", str(src)],
        check=True,
    )
    out = tmp_path / "tone.flac"
    rc, _ = _run(monkeypatch, capsys, [str(src), "-s", "1.2", "-o", str(out)])
    assert rc == 0
    pipe = read_format_tag(out, "encoder_settings")
    assert pipe is not None
    assert "source-codec=aac" in pipe
    assert "source-mode=VBR" in pipe
    m = re.search(r"source-bitrate=(\d+k)", pipe)
    assert m is not None and int(m.group(1)[:-1]) > 0
    # Lossless control: depth recorded, bitrate/mode omitted.
    lossless = tmp_path / "tone.flac"
    _sine_flac(lossless)
    lossless_out = tmp_path / "lossless.flac"
    rc, _ = _run(
        monkeypatch, capsys, [str(lossless), "-s", "1.2",
                              "-o", str(lossless_out)])
    assert rc == 0
    lossless_pipe = read_format_tag(lossless_out, "encoder_settings")
    assert lossless_pipe is not None
    assert "source-depth=" in lossless_pipe
    assert "source-bitrate=" not in lossless_pipe
    assert "source-mode=" not in lossless_pipe


def test_opus_albumartist_canonical(monkeypatch, capsys, tmp_path):
    # Template albumartist= must land as ALBUMARTIST, not the movenc
    # alias spelling ALBUM_ARTIST.
    _require_tools("ffmpeg", "ffprobe", "opusenc", "opusinfo")
    src = tmp_path / "tone.flac"
    _sine_flac(src)
    tmpl = tmp_path / "aa.tmpl"
    tmpl.write_text('albumartist="Various Artists"\n', encoding="utf-8")
    out = tmp_path / "tone.opus"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(src), "-s", "1.2", "-F", "opus",
         "--lossy-opts=--bitrate 64", "--tag-template", str(tmpl),
         "-o", str(out)],
    )
    assert rc == 0
    fields = native_format_tags(out)
    assert fields is not None
    assert fields.get("albumartist") == "Various Artists"
    assert "album_artist" not in fields


def test_m4a_albumartist_passthrough(monkeypatch, capsys, tmp_path):
    # Batch/source pass-through (no tag template): a FLAC source's
    # ALBUMARTIST must survive into an m4a output as the aART atom.
    # movenc drops every spelling except album_artist, so the pass-
    # through layer rewrites it; ffmpeg's -map_metadata path already
    # normalizes via the demuxers.
    _require_tools("ffmpeg", "ffprobe")
    src = tmp_path / "tone.flac"
    _sine_flac(src)
    subprocess.run(
        ["metaflac", "--set-tag=ALBUMARTIST=Various Artists", str(src)],
        check=True,
    )
    out = tmp_path / "tone.m4a"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(src), "-s", "1.2", "-F", "aac",
         "--lossy-opts=-b:a 64k", "-o", str(out)],
    )
    assert rc == 0
    assert read_format_tag(out, "album_artist") == "Various Artists"


def test_mp3_compilation_tcmp(monkeypatch, capsys, tmp_path):
    # Template compilation= must land as a native TCMP frame, not a
    # generic TXXX duplicate.
    mutagen_mp3 = pytest.importorskip("mutagen.mp3")
    _require_tools("ffmpeg", "ffprobe")
    src = tmp_path / "tone.flac"
    _sine_flac(src)
    tmpl = tmp_path / "c.tmpl"
    tmpl.write_text('compilation="1"\n', encoding="utf-8")
    out = tmp_path / "tone.mp3"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(src), "-s", "1.2", "-F", "mp3",
         "--tag-template", str(tmpl), "-o", str(out)],
    )
    assert rc == 0
    track = mutagen_mp3.MP3(str(out))
    assert track.get("TCMP") is not None
    assert track["TCMP"].text == ["1"]
    dupes = [k for k in track.keys()
             if k.startswith("TXXX:")
             and k.split(":", 1)[1].upper()
             in ("TCMP", "COMPILATION", "ITUNESCOMPILATION")]
    assert dupes == []


def test_aac_pipeline_toolbox(monkeypatch, capsys, tmp_path):
    # qaac encodes record the CoreAudioToolbox DLL version alongside
    # the qaac frontend version: the DLL does the actual encoding.
    _require_tools("ffmpeg", "ffprobe", "qaac")
    src = tmp_path / "tone.flac"
    _sine_flac(src)
    out = tmp_path / "tone.m4a"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(src), "-s", "1.2", "-F", "aac",
         "--lossy-opts=-V 91", "-o", str(out)],
    )
    assert rc == 0
    pipe = read_format_tag(out, "encoder_settings")
    assert pipe is not None
    assert "CoreAudioToolbox=" in pipe


def test_renametag_backfills_stamp(monkeypatch, capsys, tmp_path):
    # Old library file: filename/title tags but no machine tags at all.
    # -N rebuilds the pipeline from the filename so -D works afterwards.
    _require_fixture()
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    work = tmp_path / "work.flac"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(FIXTURE), "-s", "1.2", "--no-stamp", "--no-pipeline",
         "-o", str(work)],
    )
    assert rc == 0
    tagged = tmp_path / "old nightcore [1.2x].flac"
    work.rename(tagged)
    fixed = tmp_path / "fixed.flac"
    rc, _ = _run(monkeypatch, capsys, [str(tagged), "-N", "-o", str(fixed)])
    assert rc == 0
    fields = _flac_raw_fields(fixed)
    assert "speed=1.2x" in (fields.get("encoder_settings") or "")
    assert fields.get("DESCRIPTION") == tool_stamp()
    title = read_format_tag(fixed, "title")
    # -N changes no audio, so true shift tags stay; only the empty
    # "[Nightcore ]" artifact from the old build must be gone.
    assert title is not None and "[Nightcore ]" not in title


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
    # Single-file outputs land in the current directory: run from tmp_path
    # so the restored file lands next to its source.
    monkeypatch.chdir(tmp_path)
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


def test_identity_transcode_inherits_shift(monkeypatch, capsys, tmp_path):
    # Speed-1.0 distribution transcode of a tagged file: the inherited
    # shift stays in name, title and pipeline (a blank 1.0x would erase
    # history and break -D).
    _require_fixture()
    _require_tools("ffmpeg", "ffprobe", "metaflac")
    tagged = tmp_path / "tagged.flac"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(FIXTURE), "-s", "1.1", "--autorename", "-o", str(tagged)],
    )
    assert rc == 0
    out = tmp_path / "dist.mp3"
    rc, _ = _run(
        monkeypatch, capsys,
        [str(tagged), "-F", "mp3", "--autorename", "-o", str(out)],
    )
    assert rc == 0
    title = read_format_tag(out, "title")
    assert title is not None and "[Nightcore 1.1x]" in title
    pipe = read_format_tag(out, "encoder_settings")
    assert pipe is not None and "speed=1.1x" in pipe


def test_pitch_path_uses_tamed_wav(monkeypatch, capsys, tmp_path):
    """The pitch job must encode the wav tame_hot_wav() returns.

    Regression test: `cur = tmp3` after the tame call discarded the
    headroom trim, silently encoding the untrimmed rubberband output.
    A sentinel stands in for the trimmed file; only its duration
    can survive to the output if the wiring is correct.
    """
    _require_tools("ffmpeg", "ffprobe", "sox", "rubberband")
    import nightcore.convert as conv
    src = tmp_path / "src.wav"
    subprocess.run(
        ["sox", "-n", "-r", "48000", str(src), "synth", "2", "sine", "440"],
        check=True, capture_output=True,
    )
    sentinel = tmp_path / "sentinel.wav"
    subprocess.run(
        ["sox", "-n", "-r", "48000", str(sentinel), "synth", "1", "sine",
         "880"],
        check=True, capture_output=True,
    )
    monkeypatch.setattr(
        conv, "tame_hot_wav", lambda path, make_temp: (str(sentinel), True))
    out = tmp_path / "out.flac"
    rc, _ = _run(monkeypatch, capsys,
                 [str(src), "-S", "2", "-o", str(out)])
    assert rc == 0
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(out)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert abs(float(probe) - 1.0) < 0.1


def test_resample_redo_keeps_level(tmp_path):
    """A clipping resample is redone guarded, keeping source level.

    gain -h alone would leave a hot master ~3dB quieter; the trailing
    gain -rh reclaims exactly the reserved headroom (the pair -G
    wraps chains in), so peaks land back at full scale, never over.
    The fixture is peak-normalized in Python: sox synth alone does
    not reliably hit full scale.
    """
    if shutil.which("sox") is None:
        pytest.skip("sox not found")
    import array
    import math
    import wave
    from nightcore.backends import run_sox_rate
    raw = array.array("h")
    for i in range(44100 * 2):
        raw.append(int(32767 * math.sin(2 * math.pi * 997 * i / 44100)))
    peak = max(abs(v) for v in raw)
    raw = array.array("h", (int(v * 32767 / peak) for v in raw))
    src = tmp_path / "hot.wav"
    with wave.open(str(src), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(raw.tobytes())
    out = tmp_path / "out.wav"
    run_sox_rate(src, out, 48000)
    probe = subprocess.run(
        ["sox", str(out), "-n", "stats"],
        check=True, capture_output=True, text=True,
    ).stderr
    match = re.search(r"(?m)^Pk lev dB\s+(.+)$", probe)
    assert match is not None
    out_peak = max(float(t) for t in re.findall(r"-?\d+(?:\.\d+)?",
                                                match.group(1)))
    assert -1.0 <= out_peak <= 0.0
