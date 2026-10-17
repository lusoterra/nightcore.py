"""--mkvid: remux one song (+ optional .lrc lyrics, + image) into a
1080p YouTube video.

The image is letterboxed / pillarboxed into 1920x1080 with black bars
(never stretched); the audio is remuxed losslessly -- FLAC in mkv, ALAC
in mp4 -- so YouTube's Opus transcode is a single generation of loss.
There is no resampling (mkv/mp4 + FLAC/ALAC accept any sample rate); if
that ever changes, SoX does the resampling.

No audio processing flags (-s, -S, -p, -r, -R, -D) apply: this is a
remux, not a conversion.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .audio import cover_codec_name, extract_cover_tmp, probe_audio
from .constants import (
    AUDIO_EXTS,
    _IMAGE_MIME,
    MKVID_CRF,
    MKVID_DEFAULT,
    MKVID_FORMATS,
    MKVID_HEIGHT,
    MKVID_PRESET,
    MKVID_TUNE,
    MKVID_WIDTH,
)
from .naming import single_parent
from .tags import read_tags, sanitize_filename, tool_stamp
from .util import run_tool, say

# LRC timestamp: [mm:ss.cc] with optional [mm:ss.ccc]; the rest of the
# line is the lyric text. Metadata headers ([ar:], [ti:] ...) never match
# because they have a letter right after the colon... actually they don't
# -- this pattern requires digits before the colon, so headers are safe.
_LRC_LINE = re.compile(r"^\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\](.*)$")


def _lrc_frac_to_ms(frac):
    """Fractional LRC digits to milliseconds."""
    if frac is None:
        return 0
    if len(frac) == 1:
        return int(frac) * 100
    if len(frac) == 2:
        return int(frac) * 10
    return int(frac[:3])


def _ms_to_srt(ms):
    """Milliseconds -> SRT timestamp (hours:minutes:seconds,millis)."""
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(lrc_path, srt_path, duration_ms=None):
    """Convert an .lrc lyric file to SubRip (.srt) text at srt_path.

    Each timestamped line becomes one cue running until the next
    timestamp; the last cue runs 5 seconds or to the song's duration.
    Never raises (unreadable file -> empty file).
    """
    try:
        raw = lrc_path.read_bytes()
    except OSError:
        srt_path.write_text("", encoding="utf-8")
        return
    text = raw.decode("utf-8-sig")
    entries = []
    for line in text.splitlines():
        m = _LRC_LINE.match(line)
        if not m:
            continue
        mm, ss, frac, txt = m.groups()
        start = (int(mm) * 60 + int(ss)) * 1000 + _lrc_frac_to_ms(frac)
        txt = txt.strip()
        if txt:
            entries.append((start, txt))
    cues = []
    last = len(entries) - 1
    for i, (start, txt) in enumerate(entries):
        if i < last:
            end = entries[i + 1][0]
        elif duration_ms is not None:
            end = duration_ms
        else:
            end = start + 5000
        if end <= start:
            end = start + 1000
        cues.append((start, end, txt))
    out = []
    for n, (start, end, txt) in enumerate(cues, 1):
        out.append(str(n))
        out.append(f"{_ms_to_srt(start)} --> {_ms_to_srt(end)}")
        out.append(txt)
        out.append("")
    srt_path.write_text("\n".join(out), encoding="utf-8")


def run_mkvid(args):
    """Remux one song into a 1080p YouTube video. Exit code, like main."""
    # Egregious: contradict the remux contract (one song, no audio
    # processing, no batching). These would silently do nothing, so they
    # fail loudly instead of producing a misleading output.
    egregious = (
        ("--batch", args.batch is not None),
        ("-s/--speed", args.speed != 1.0),
        ("-S/--semitones", args.semitones is not None),
        ("-p/--pitch", args.pitch),
        ("-r/--reciprocal", args.reciprocal),
        ("-R/--resample", args.resample),
        ("-D/--denightcore", args.denightcore),
        ("--renametag", args.renametag),
        ("--in-place", args.in_place),
    )
    for flag, bad in egregious:
        if bad:
            print(
                f"Error: --mkvid cannot be combined with {flag} "
                "(it remuxes, it does not process audio)",
                file=sys.stderr,
            )
            return 1
    # Harmless passengers a shell alias may carry (--quiet, --autorename,
    # --tag-template, --no-delete-tags): mkvid names and tags the video
    # itself from the song's tags, so these shape nothing here and are
    # silently ignored rather than refused.

    fmt = args.format
    if fmt == "flac":  # the bare default; --mkvid's own default is mkv
        fmt = MKVID_DEFAULT
    if fmt not in MKVID_FORMATS:
        print(
            f"Error: --mkvid needs -F mkv or -F mp4 (got {fmt!r})",
            file=sys.stderr,
        )
        return 1
    spec = MKVID_FORMATS[fmt]
    ext = spec["ext"]
    audio_codec = spec["audio_codec"]
    sub_codec = spec["subtitle_codec"]

    inputs = [Path(p) for p in args.input]
    audio_files = [
        p for p in inputs
        if p.is_file() and p.suffix.lower() != ".lrc"
        and p.suffix.lower() in AUDIO_EXTS
    ]
    lrcs = [p for p in inputs if p.suffix.lower() == ".lrc"]
    others = [
        p for p in inputs
        if p not in audio_files and p not in lrcs
    ]
    if others:
        print(
            "Error: --mkvid takes a song (+ one optional .lrc lyric), "
            f"not: {', '.join(str(p) for p in others)}",
            file=sys.stderr,
        )
        return 1
    if len(audio_files) != 1:
        print("Error: --mkvid needs exactly one song input", file=sys.stderr)
        return 1
    if len(lrcs) > 1:
        print("Error: --mkvid takes at most one .lrc lyric file",
              file=sys.stderr)
        return 1
    song = audio_files[0]
    lrc_path = lrcs[0] if lrcs else None

    if not song.is_file():
        print(f"Error: File not found: {song}", file=sys.stderr)
        return 1

    tempdir = None
    try:
        # --- image: --cover wins; else the song's embedded cover art;
        #     no image at all is a loud failure ----------------------
        image = None
        if args.cover:
            cover_path = Path(args.cover)
            if not cover_path.is_file():
                print(f"Error: cover image not found: {cover_path}",
                      file=sys.stderr)
                return 1
            if cover_path.suffix.lower() not in _IMAGE_MIME:
                print(
                    f"Error: --cover needs a PNG or JPG image: {cover_path}",
                    file=sys.stderr,
                )
                return 1
            image = cover_path
        else:
            cc = cover_codec_name(song)
            if cc in ("mjpeg", "png"):
                tempdir = Path(tempfile.mkdtemp(prefix="nightcore-mkvid-"))
                image = extract_cover_tmp(song, cc, tempdir)
        if image is None:
            print(
                "Error: --mkvid needs an image (--cover IMAGE.png/.jpg).\n"
                f"The song '{song.name}' has no embedded cover art, so add "
                "an image with --cover.",
                file=sys.stderr,
            )
            return 1

        # --- output path --------------------------------------------
        parent = single_parent(args, song)
        if args.output is not None:
            output_path = parent / args.output
        else:
            output_path = parent / (sanitize_filename(song.stem) + ext)
        if output_path == song:
            print("Error: output would overwrite input, aborting",
                  file=sys.stderr)
            return 1
        if output_path.exists() and not args.overwrite:
            print(
                f"Error: output already exists: {output_path} "
                "(use -y to overwrite)",
                file=sys.stderr,
            )
            return 1

        if args.dry_run:
            extra = " and lyrics embedded" if lrc_path is not None else ""
            say(f"{output_path.name}: would mux {song.name} "
                f"with {image.name} into {fmt} ({audio_codec} audio, "
                f"1080p letterboxed{extra})")
            return 0

        # --- lyrics: .lrc -> .srt, embedded as a subtitle track -----
        srt_path = None
        if lrc_path is not None:
            if tempdir is None:
                tempdir = Path(tempfile.mkdtemp(prefix="nightcore-mkvid-"))
            srt_path = tempdir / f"{lrc_path.stem}.srt"
            duration_ms = None
            try:
                duration_ms = int(float(
                    probe_audio(song)["format"].get("duration", 0) or 0) * 1000)
            except (TypeError, ValueError):
                duration_ms = None
            write_srt(lrc_path, srt_path, duration_ms)

        # --- ffmpeg remux -------------------------------------------
        cmd = ["ffmpeg", "-nostdin",
               "-y" if args.overwrite else "-n",
               "-loop", "1", "-i", str(image),
               "-i", str(song)]
        if srt_path is not None:
            cmd += ["-i", str(srt_path)]
        vf = (
            f"[0:v]scale={MKVID_WIDTH}:{MKVID_HEIGHT}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={MKVID_WIDTH}:{MKVID_HEIGHT}:"
            f"(ow-iw)/2:(oh-ih)/2:black,"
            f"format=yuv420p[vid]"
        )
        cmd += ["-filter_complex", vf]
        cmd += ["-map", "[vid]", "-map", "1:a"]
        if srt_path is not None:
            cmd += ["-map", "2:s", "-c:s", sub_codec]
        cmd += [
            "-c:v", "libx264", "-preset", MKVID_PRESET, "-tune",
            MKVID_TUNE, "-crf", MKVID_CRF,
            "-c:a", audio_codec,
            "-shortest",
        ]
        if fmt == "mp4":
            cmd += ["-movflags", "+faststart"]
        # All metadata from the song, then make sure title/artist land.
        cmd += ["-map_metadata", "1"]
        tags = read_tags(
            song, "title", "artist", "album", "date", "genre",
            "comment", "description",
        )
        title = tags.get("title") or song.stem
        artist = tags.get("artist")
        cmd += ["-metadata", f"title={title}"]
        if artist:
            cmd += ["-metadata", f"artist={artist}"]
        for key in ("album", "date", "genre"):
            value = tags.get(key)
            if value:
                cmd += ["-metadata", f"{key}={value}"]
        if not getattr(args, "no_stamp", False):
            cmd += [
                "-metadata",
                f"description={tool_stamp()} --mkvid",
            ]
        cmd += [str(output_path)]

        say(f"{output_path.name}: muxing {song.name} -> {fmt} "
            f"({audio_codec} audio, 1080p no stretch)")
        run_tool(cmd)
        say(f"  done: {output_path}")
        return 0

    except subprocess.CalledProcessError as e:
        print(f"\nffmpeg failed with exit code {e.returncode}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("Error: ffmpeg went missing mid-run.", file=sys.stderr)
        return 127
    finally:
        if tempdir is not None:
            shutil.rmtree(tempdir, ignore_errors=True)
