"""--mkvid: remux one song (+ optional .lrc lyrics, + image) into a
1080p YouTube video, or speed an existing video up.

Still-image mode (song input): the image is letterboxed / pillarboxed into
1920x1080 with black bars (never stretched); the audio rides as FLAC in mkv,
ALAC in mp4alac, or AAC in mp4 (default, qaac TVBR 91 unless --lossy-opts
overrides) -- so mkv/mp4alac reach YouTube's Opus transcode as a single
generation of loss, while mp4 trades one extra lossy generation for players
that reject ALAC (Discord and friends) and containers they reject (mkv).
No audio processing flags apply in this mode: it is a remux.

Video-speedup mode (input with a real video stream, not cover art): the
video is slowed/sped with setpts and the audio with the normal nightcore
asetrate chain (-s, -S, -p, -r, -R, -D apply to the audio), re-encoded to
h264/avc at the source resolution with FLAC in mkv, ALAC in mp4alac, or AAC
in mp4. Subtitles are retimed by the same factor and kept; attached cover
art is dropped. There is no resampling unless -R is given.
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
    has_real_video,
    next_standard_rate,
    probe_audio,
    real_video_streams,
    speed_from_semitones,
    wav_codec_for,
)
from .backends import (
    detect_lossy_dialect,
    run_qaac_alac,
    select_lossy_backend,
)
from .constants import (
    AUDIO_EXTS,
    VIDEO_EXTS,
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
from .tags import read_shift_source, read_tags, sanitize_filename, tool_stamp
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


def _speedup_plan(args, song):
    """(duration_factor, pitch_extra, sample_rate, denight_raw) or ValueError.

    Mirrors the speed math in cli.main/convert: -s sets the duration
    factor (asetrate = plain nightcore, pitch follows speed); -S alone is
    pitch-only (asetrate + aresample back, duration kept); -s + -S stacks
    (duration from -s, extra pitch preserved-duration after it); -p forces
    pitch-only; -D inverts the stamped shift. Pitch is never preserved:
    a plain speed change raises/lowers pitch like the audio path does.
    """
    import math

    speed_raw = args.speed if args.speed is not None else 1.0
    eff_st = args.semitones
    recip = bool(getattr(args, "reciprocal", False))
    denight_raw = ""
    if getattr(args, "denightcore", False):
        stamped, denight_raw = read_shift_source(song)
        if stamped is None or (stamped[0] == 1.0 and stamped[1] == 0.0):
            raise ValueError(
                "magic number not found: --denightcore needs a file "
                "stamped by nightcore (filenames alone are not trusted)")
        d_speed, d_st = stamped
        duration_factor = 1.0 / d_speed if d_speed else 1.0
        pitch_extra = speed_from_semitones(-d_st)
    elif eff_st is not None and speed_raw != 1.0:
        # Combo: duration from -s, extra semitone pitch on top.
        st_signed = -eff_st if recip else eff_st
        duration_factor = (1.0 / speed_raw) if recip else speed_raw
        pitch_extra = speed_from_semitones(st_signed)
    elif eff_st is not None:
        pitch_factor = speed_from_semitones(eff_st)
        if recip:
            pitch_factor = 1.0 / pitch_factor
        duration_factor = 1.0
        pitch_extra = pitch_factor
    else:
        duration_factor = (1.0 / speed_raw) if recip else speed_raw
        pitch_extra = 1.0
    if getattr(args, "pitch", False) and not getattr(args, "denightcore", False):
        # -p: pitch-shift only, keep duration (amount comes from -s/-S).
        if eff_st is not None and speed_raw != 1.0:
            pitch_extra = duration_factor * pitch_extra
        elif eff_st is None:
            pitch_extra = duration_factor
        duration_factor = 1.0
    if duration_factor <= 0 or not math.isfinite(duration_factor):
        raise ValueError("Speed factor must be a finite number > 0")
    if not math.isfinite(pitch_extra) or pitch_extra <= 0:
        raise ValueError("Pitch factor must be a finite number > 0")
    try:
        sample_rate = int(str(
            probe_audio(song)["audio"].get("sample_rate")).strip())
    except (ValueError, AttributeError, TypeError):
        sample_rate = None
    return duration_factor, pitch_extra, sample_rate, denight_raw


def _audio_speed_chain(sample_rate, duration_factor, pitch_extra, args):
    """asetrate chain matching _speedup_plan; None means pass through."""
    if sample_rate is None:
        if duration_factor == 1.0 and pitch_extra == 1.0:
            return None
        raise ValueError("could not determine input sample rate")
    sr = sample_rate
    if pitch_extra != 1.0 and duration_factor != 1.0:
        base = sr * duration_factor
        chain = (f"asetrate={base:.2f},aresample={int(round(base))}"
                 f",asetrate={base * pitch_extra:.2f},"
                 f"aresample={int(round(base))}")
    elif pitch_extra != 1.0:
        chain = f"asetrate={sr * pitch_extra:.2f},aresample={sr}"
    elif duration_factor != 1.0:
        chain = f"asetrate={sr * duration_factor:.2f}"
    else:
        chain = None
    if getattr(args, "resample", False):
        target = next_standard_rate(sr * duration_factor)
        if target is not None:
            chain = (f"{chain},aresample={target}" if chain
                     else f"aresample={target}")
    return chain


def _run_mkvid_video(args, song, fmt, spec, ext, audio_codec, sub_codec,
                     is_lossy, lossy_opts, backend, use_qaac, lrc_path):
    """Speed an existing video up: setpts video, asetrate audio, h264 out.

    Resolution and fps are kept (no 1080p letterbox); subtitles are
    retimed by the duration factor and kept; every audio track is sped.
    Returns an exit code, like run_mkvid.
    """
    try:
        duration_factor, pitch_extra, sample_rate, denight_raw = \
            _speedup_plan(args, song)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    probe = probe_audio(song)
    n_audio = len(probe.get("audio_streams") or [])
    if n_audio == 0 and probe.get("audio"):
        n_audio = 1
    n_subs = len(probe.get("subtitles") or [])
    try:
        audio_chain = (_audio_speed_chain(
            sample_rate, duration_factor, pitch_extra, args)
            if n_audio else None)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    parent = single_parent(args, song)
    if args.output is not None:
        output_path = parent / args.output
    else:
        suffix = ""
        if duration_factor != 1.0:
            suffix = f" [{duration_factor:g}x]"
        elif pitch_extra != 1.0:
            suffix = ""
        output_path = parent / (sanitize_filename(song.stem + suffix) + ext)
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
        say(f"{output_path.name}: would speed {song.name} "
            f"{duration_factor:g}x -> {fmt} ({audio_codec} audio, "
            f"h264{', subtitles retimed' if n_subs else ''})")
        return 0

    tempdir = None
    try:
        # --- mp4 AAC via qaac: encode the sped audio first, then mux
        # the sped video onto it (audio is stream-copied in the mux).
        aac_tmp = None
        if use_qaac and n_audio:
            tempdir = Path(tempfile.mkdtemp(prefix="nightcore-mkvid-"))
            _codec, _br, _bits, _fbr, _enc = get_audio_info(song)
            depth_pcm = wav_codec_for(_bits)
            tmp_wav = tempdir / "mkvid.wav"
            aac_tmp = tempdir / "mkvid-aac.m4a"
            say(f"  ffmpeg decode -> {depth_pcm} (sped {duration_factor:g}x)")
            decode_cmd = ["ffmpeg", "-nostdin", "-y",
                          "-i", str(song),
                          "-map", "0:a", "-c:a", depth_pcm]
            if audio_chain:
                # qaac/CoreAudio rejects sped (non-standard) rates, so
                # normalize the temp WAV back to the source rate. This
                # keeps the sped duration and pitch (aresample preserves
                # both while changing the container rate).
                decode_chain = audio_chain
                if sample_rate:
                    decode_chain += f",aresample={sample_rate}"
                decode_cmd += ["-af", decode_chain]
            decode_cmd += [str(tmp_wav)]
            # Decode filter must be -filter:a; -af is its alias.
            run_tool(decode_cmd)
            say(f"  qaac encode aac ({' '.join(lossy_opts)})")
            run_qaac_alac(tmp_wav, aac_tmp, alac=False,
                          extra_args=lossy_opts)
            try:
                tmp_wav.unlink()
            except OSError:
                pass

        # --- ffmpeg: setpts video (+ retimed subtitles), asetrate audio
        v_parts = "format=yuv420p"
        fc = []
        if duration_factor != 1.0:
            fc.append(f"[0:v:0]setpts=PTS/{duration_factor:g},{v_parts}[vout]")
        else:
            fc.append(f"[0:v:0]{v_parts}[vout]")
        cmd = ["ffmpeg", "-nostdin",
               "-y" if args.overwrite else "-n",
               "-i", str(song)]
        if aac_tmp is not None:
            cmd += ["-i", str(aac_tmp)]
        audio_labels = []
        audio_indices = list(range(n_audio))
        for i in audio_indices:
            label = f"aout{i}"
            audio_labels.append(label)
            if aac_tmp is not None:
                fc.append(f"[1:a:0]anull[{label}]")
            elif audio_chain:
                fc.append(f"[0:a:{i}]{audio_chain}[{label}]")
            else:
                fc.append(f"[0:a:{i}]anull[{label}]")
        cmd += ["-filter_complex", ";".join(fc)]
        cmd += ["-map", "[vout]"]
        for label in audio_labels:
            cmd += ["-map", f"[{label}]"]
        # Subtitles are not video/audio: setpts would turn them into a
        # second video stream. Retiming goes through the setts bitstream
        # filter instead, which scales TS and DURATION by the same factor.
        for i in range(n_subs):
            cmd += ["-map", f"0:s:{i}"]
        cmd += ["-c:v", "libx264", "-preset", MKVID_PRESET, "-crf",
                MKVID_CRF, "-pix_fmt", "yuv420p"]
        if aac_tmp is not None:
            cmd += ["-c:a", "copy"]
        elif is_lossy:
            cmd += ["-c:a", audio_codec] + list(lossy_opts)
        else:
            cmd += ["-c:a", audio_codec]
        if n_subs:
            cmd += ["-c:s", sub_codec]
            if duration_factor != 1.0:
                cmd += ["-bsf:s",
                        f"setts=ts=TS/{duration_factor:g}"
                        f":duration=DURATION/{duration_factor:g}"]
        if ext == ".mp4":
            cmd += ["-movflags", "+faststart"]
        cmd += ["-map_metadata", "0"]
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
                f"description={tool_stamp()} --mkvid {duration_factor:g}x",
            ]
        cmd += [str(output_path)]

        suffix = f" -> {fmt} ({audio_codec} audio, h264)"
        glue = ": speeding "
        width = _term_width()
        song_room = max(10, (width - len(glue) - len(suffix)) * 2 // 3)
        out_room = max(10, width - len(glue) - len(suffix) - song_room)
        line = (f"{_abbrev(output_path.name, out_room)}{glue}"
                f"{_abbrev(song.name, song_room)} {duration_factor:g}x{suffix}")
        say(line[:width])
        say(f"  ffmpeg video speedup {duration_factor:g}x")
        run_tool(cmd)
        say(f"  done: {output_path}")
        if _quiet < 2:
            print(f"Summary: 1 converted -> {output_path}")
        return 0

    except subprocess.CalledProcessError as e:
        print(f"\nffmpeg/qaac failed with exit code {e.returncode}",
              file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("Error: ffmpeg went missing mid-run.", file=sys.stderr)
        return 127
    finally:
        if tempdir is not None:
            shutil.rmtree(tempdir, ignore_errors=True)


def run_mkvid(args):
    """Remux one song into a 1080p YouTube video, or speed an existing
    video up. Exit code, like main."""
    # Egregious: contradict the remux contract (one song, no audio
    # processing, no batching). The speed/pitch family is legal again
    # for a real-video input (it drives the speedup) and is rejected
    # for still-image mode below, once the probe knows which we have.
    egregious = (
        ("--batch", args.batch is not None),
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
        and p.suffix.lower() in (AUDIO_EXTS + VIDEO_EXTS)
    ]
    lrcs = [p for p in inputs if p.suffix.lower() == ".lrc"]
    others = [
        p for p in inputs
        if p not in audio_files and p not in lrcs
    ]
    if others:
        print(
            "Error: --mkvid takes a song or video (+ one optional .lrc lyric), "
            f"not: {', '.join(str(p) for p in others)}",
            file=sys.stderr,
        )
        return 1
    if len(audio_files) != 1:
        print("Error: --mkvid needs exactly one song or video input",
              file=sys.stderr)
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

    # Video-speedup vs still-image mode, decided by the probe: cover art
    # (attached_pic / mjpeg / png) never counts as a video stream.
    is_video = has_real_video(song)
    if not is_video:
        egregious_speed = (
            ("-s/--speed", args.speed != 1.0),
            ("-S/--semitones", args.semitones is not None),
            ("-p/--pitch", args.pitch),
            ("-r/--reciprocal", args.reciprocal),
            ("-R/--resample", args.resample),
            ("-D/--denightcore", args.denightcore),
        )
        for flag, bad in egregious_speed:
            if bad:
                print(
                    f"Error: --mkvid cannot be combined with {flag} "
                    "(still-image mode remuxes, it does not process audio; "
                    "use a video input to speed a video up)",
                    file=sys.stderr,
                )
                return 1
    if is_video:
        return _run_mkvid_video(
            args, song, fmt, spec, ext, audio_codec, sub_codec,
            is_lossy, lossy_opts, backend, use_qaac, lrc_path)

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
