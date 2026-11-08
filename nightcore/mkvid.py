"""--mkvid: remux one song (+ optional .lrc lyrics, + image) into a
1080p YouTube video.

The image is letterboxed / pillarboxed into 1920x1080 with black bars
(never stretched); the audio rides as FLAC in mkv, ALAC in mp4alac, or
AAC in mp4 (default, qaac TVBR 91 unless --lossy-opts overrides) -- so
mkv/mp4alac reach YouTube's Opus transcode as a single generation of
loss, while mp4 trades one extra lossy generation for players that
reject ALAC (Discord and friends) and containers they reject (mkv).
There is no resampling (the containers accept the source rate); if
that ever changes, SoX does the resampling.

No audio processing flags (-s, -S, -p, -r, -R, -D) apply: this is a
remux, not a conversion (mp4's AAC encode is the one exception, and it
takes no shift either).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .audio import (
    cover_codec_name,
    extract_cover_tmp,
    get_audio_info,
    probe_audio,
    wav_codec_for,
)
from .backends import (
    detect_lossy_dialect,
    run_qaac_alac,
    select_lossy_backend,
)
from .constants import (
    AUDIO_EXTS,
    _IMAGE_MIME,
    LOSS_DEFAULTS,
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
from .util import _quiet, run_tool, say

# LRC timestamp: [mm:ss.cc] with optional [mm:ss.ccc]; the rest of the
# line is the lyric text. Metadata headers ([ar:], [ti:] ...) never match
# because they have a letter right after the colon... actually they don't
# -- this pattern requires digits before the colon, so headers are safe.
_LRC_LINE = re.compile(r"^\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\](.*)$")


def _term_width():
    """Columns of the stderr terminal (80 fallback).

    Measured on stderr itself: that is where the log line draws, and
    stdout may be wider, narrower, or redirected. Same approach as the
    stats progress tick, so narrow or redirected terminals never wrap.
    """
    try:
        width = os.get_terminal_size(sys.stderr.fileno()).columns
        if width >= 20:
            return width
    except (OSError, AttributeError, ValueError):
        pass
    try:
        return max(20, shutil.get_terminal_size().columns)
    except OSError:
        return 80


def _abbrev(name, width=None):
    """Collapse whitespace and tail-truncate a path/name to fit `width`
    (stderr columns, default), keeping the tail (suffixes tell files
    apart)."""
    if width is None:
        width = _term_width()
    shown = " ".join(str(name).split())
    if len(shown) > width:
        shown = "..." + shown[-(width - 3):] if width > 3 else "..."
    return shown


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
            f"Error: --mkvid needs -F mkv, -F mp4 or -F mp4alac (got {fmt!r})",
            file=sys.stderr,
        )
        return 1
    spec = MKVID_FORMATS[fmt]
    ext = spec["ext"]
    audio_codec = spec["audio_codec"]
    sub_codec = spec["subtitle_codec"]
    is_lossy = bool(spec.get("lossy"))

    # --lossy-opts shapes mp4's AAC encode only (same dialect rules and
    # V91 default as -F aac): qaac-style flags pick qaac, -X:a style picks
    # ffmpeg. Lossless containers (mkv, mp4alac) ignore them with the
    # usual warning.
    lossy_opts = []
    if getattr(args, "lossy_opts", None) is not None:
        try:
            lossy_opts = shlex.split(args.lossy_opts)
        except ValueError as e:
            print(f"Error: could not parse --lossy-opts: {e}", file=sys.stderr)
            return 1
    if lossy_opts and not is_lossy:
        print("Warning: --lossy-opts ignored for lossless output",
              file=sys.stderr)
        lossy_opts = []
    backend = None
    if is_lossy:
        try:
            dialect = detect_lossy_dialect("aac", lossy_opts)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        backend = select_lossy_backend("aac", dialect)
        if backend is None:
            return 127
        if not lossy_opts:
            default = LOSS_DEFAULTS[("aac", backend["kind"])]
            if default is None:
                print("Error: ffmpeg AAC has no default (it isn't that "
                      "great); pass explicit --lossy-opts, e.g. "
                      "--lossy-opts=-b:a 160k (or better: install qaac)",
                      file=sys.stderr)
                return 1
            lossy_opts = list(default)
            say(f"Default lossy opts for {fmt} "
                f"via {backend['kind']}: {' '.join(default)}")
    use_qaac = is_lossy and backend is not None and backend.get("kind") == "qaac"

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
            width = _term_width()
            head = ": would mux "
            mid = " with "
            tail = f" into {fmt} ({audio_codec} audio, 1080p h264{extra})"
            fixed = len(head) + len(mid) + len(tail)
            room = max(30, width - fixed)
            share = max(10, room // 3)
            line = (
                f"{_abbrev(output_path.name, share)}{head}"
                f"{_abbrev(song.name, share)}{mid}"
                f"{_abbrev(image.name, share)}{tail}"
            )
            say(line[:width])
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

        # --- mp4 AAC via qaac: decode + CoreAudio encode first -----
        # qaac is audio-only (never tags: those ride the final mux from
        # the song itself), so its output becomes a second audio input
        # below while the song stays on as the metadata source.
        aac_tmp = None
        if use_qaac:
            if tempdir is None:
                tempdir = Path(tempfile.mkdtemp(prefix="nightcore-mkvid-"))
            _codec, _br, _bits, _fbr, _enc = get_audio_info(song)
            depth_pcm = wav_codec_for(_bits)
            tmp_wav = tempdir / "mkvid.wav"
            aac_tmp = tempdir / "mkvid-aac.m4a"
            say(f"  ffmpeg decode -> {depth_pcm}")
            run_tool(["ffmpeg", "-nostdin", "-y",
                      "-i", str(song),
                      "-map", "0:a", "-c:a", depth_pcm,
                      str(tmp_wav)])
            say(f"  qaac encode aac ({' '.join(lossy_opts)})")
            run_qaac_alac(tmp_wav, aac_tmp, alac=False,
                           extra_args=lossy_opts)
            try:
                tmp_wav.unlink()
            except OSError:
                pass

        # --- ffmpeg remux -------------------------------------------
        # Input layout is [image, audio, (song-as-metadata,) (srt,)]:
        # lossless and ffmpeg-AAC mux the song directly (audio=1,
        # meta=1, srt=2); qaac muxes its temp m4a (audio=1, meta=2,
        # srt=3) so tags still come from the song, never the bare temp.
        cmd = ["ffmpeg", "-nostdin",
               "-y" if args.overwrite else "-n",
               "-loop", "1", "-i", str(image),
               "-i", str(aac_tmp if use_qaac else song)]
        meta_idx = "1"
        if use_qaac:
            cmd += ["-i", str(song)]
            meta_idx = "2"
        srt_idx = None
        if srt_path is not None:
            # Input indices so far: 0=image, 1=audio, [2=song]; the srt
            # lands next, so its index is the current input count.
            srt_idx = "2" if not use_qaac else "3"
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
            assert srt_idx is not None
            cmd += ["-map", f"{srt_idx}:s", "-c:s", sub_codec]
        cmd += [
            "-c:v", "libx264", "-preset", MKVID_PRESET, "-tune",
            MKVID_TUNE, "-crf", MKVID_CRF,
        ]
        if use_qaac:
            # Already AAC: stream-copy the qaac output, no second encode.
            cmd += ["-c:a", "copy"]
        elif is_lossy:
            cmd += ["-c:a", audio_codec] + list(lossy_opts)
        else:
            cmd += ["-c:a", audio_codec]
        cmd += ["-shortest"]
        if ext == ".mp4":
            cmd += ["-movflags", "+faststart"]
        # All metadata from the song, then make sure title/artist land.
        cmd += ["-map_metadata", meta_idx]
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

        width = _term_width()
        suffix = f" -> {fmt} ({audio_codec} audio, 1080p h264)"
        glue = ": muxing "
        room = max(10, width - len(glue) - len(suffix))
        # Split the remaining room between the two names, tail-truncated
        # like --stats, so long file names never wrap the line.
        song_room = max(10, room * 2 // 3)
        out_room = max(10, room - song_room)
        line = (
            f"{_abbrev(output_path.name, out_room)}{glue}"
            f"{_abbrev(song.name, song_room)}{suffix}"
        )
        say(line[:width])
        say("  ffmpeg remux")
        run_tool(cmd)
        say(f"  done: {output_path}")
        if _quiet < 2:
            print(f"Summary: 1 converted -> {output_path}")
        return 0

    except subprocess.CalledProcessError as e:
        print(f"\nffmpeg/qaac failed with exit code {e.returncode}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("Error: ffmpeg went missing mid-run.", file=sys.stderr)
        return 127
    finally:
        if tempdir is not None:
            shutil.rmtree(tempdir, ignore_errors=True)
