"""Video speedup: treat a real-video input like an audio input.

The video is slowed/sped with setpts, the audio with the normal
nightcore asetrate chain (-s, -S, -p, -r, -R, -D), subtitles are
retimed (embedded via setts, sidecar .lrc via run_lrc elsewhere),
and the result is h264/av1/vp9 + the chosen audio codec in a guessed
container. Pitch is never preserved: a plain speed change raises or
lowers pitch exactly like the audio path does.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

from . import __version__
from .audio import (
    get_audio_info,
    has_real_video,
    next_standard_rate,
    probe_audio,
    speed_from_semitones,
    wav_codec_for,
)
from .backends import (
    detect_lossy_dialect,
    ffmpeg_has_encoder,
    run_qaac_alac,
    select_lossy_backend,
)
from .constants import (
    AUDIO_CODECS,
    LOSS_DEFAULTS,
    MKVID_CRF,
    MKVID_PRESET,
    VIDEO_CODECS,
    VIDEO_ENCODERS,
    VIDEO_FORMATS,
)
from .naming import single_parent
from .tags import (
    apply_template_tags,
    compute_new_title,
    get_artist,
    get_title,
    read_shift_source,
    read_tags,
    render_autorename,
    sanitize_filename,
    tool_stamp,
)
from .util import _quiet, run_tool, say


def guess_container(vcodec, acodec):
    """(container_key, ext) for a video+audio codec pair."""
    v = vcodec.lower()
    a = acodec.lower()
    if a == "flac":
        return "mkv", ".mkv"
    if v in ("vp9", "vp8") and a in ("opus", "vorbis"):
        return "webm", ".webm"
    if v in ("vp9", "vp8", "av1") and a in ("opus", "vorbis"):
        return "webm", ".webm"
    if a == "alac":
        return "mp4", ".mp4"
    if v in ("h264", "avc", "hevc", "h265"):
        return "mp4", ".mp4"
    return "mkv", ".mkv"


EXT_TO_VIDEO_PRESET = {
    ".mp4": "mp4",
    ".mkv": "mkv",
    ".webm": "webm",
}

# Container checked against an explicit -F pair/preset: only these
# codecs can ride in each container (others fail loudly instead of
# surfacing as ffmpeg's "Error opening output file ... Invalid argument").
SUPPORTED_CONTAINER_CODECS = {
    ".mp4": ({"h264", "avc", "hevc", "h265"}, {"aac", "alac", "mp3"}),
    ".mkv": ({"h264", "avc", "hevc", "h265", "av1", "vp9", "vp8"},
             {"flac", "alac", "aac", "opus", "vorbis", "mp3"}),
    ".webm": ({"vp9", "vp8", "av1"}, {"opus", "vorbis"}),
}


def default_video_format_from_ext(ext):
    """Preset name defaulted from an -o container extension, or None."""
    return EXT_TO_VIDEO_PRESET.get(ext.lower())


EXT_TO_AUDIO_FORMAT = {
    ".flac": "flac",
    ".mp3": "mp3",
    ".opus": "opus",
    ".m4a": "alac",
}

# Audio formats we mux: which -o extensions each -F may land in.
AUDIO_FORMAT_EXTS = {
    "flac": {".flac", ".mkv"},
    "alac": {".m4a", ".mp4"},
    "mp3": {".mp3"},
    "opus": {".opus", ".mkv", ".webm"},
    "aac": {".m4a", ".mp4"},
}


def parse_video_format(token):
    """Resolve a -F token to (vcodec, acodec, container_key, ext) or None.

    Accepts preset names (mp4, mkv, mp4alac, webm) and the reversible
    slash pair (h264/aac or aac/h264 -- order does not matter, the
    known video/audio codec sets decide which is which).
    """
    low = token.strip().lower()
    if low in VIDEO_FORMATS:
        spec = VIDEO_FORMATS[low]
        return spec["video"], spec["audio"], low, spec["ext"]
    if "/" in low:
        parts = [p.strip() for p in low.split("/") if p.strip()]
        if len(parts) != 2:
            return None
        a, b = parts
        if a in VIDEO_CODECS and b in AUDIO_CODECS:
            v, ac = a, b
        elif b in VIDEO_CODECS and a in AUDIO_CODECS:
            v, ac = b, a
        else:
            return None
        key, ext = guess_container(v, ac)
        return v, ac, key, ext
    return None


def _speedup_plan(args, song):
    """(duration_factor, pitch_extra, sample_rate) or ValueError."""
    import math

    speed_raw = args.speed if args.speed is not None else 1.0
    eff_st = args.semitones
    recip = bool(getattr(args, "reciprocal", False))
    if getattr(args, "denightcore", False):
        stamped, _raw = read_shift_source(song)
        if stamped is None or (stamped[0] == 1.0 and stamped[1] == 0.0):
            raise ValueError(
                "magic number not found: --denightcore needs a file "
                "stamped by nightcore (filenames alone are not trusted)")
        d_speed, d_st = stamped
        duration_factor = 1.0 / d_speed if d_speed else 1.0
        pitch_extra = speed_from_semitones(-d_st)
    elif eff_st is not None and speed_raw != 1.0:
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
    return duration_factor, pitch_extra, sample_rate


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


def _term_width():
    import os
    try:
        width = os.get_terminal_size(sys.stderr.fileno()).columns
        if width >= 20:
            return width
    except (OSError, AttributeError, ValueError):
        pass
    return 80


def run_video_conversion(args, input_path, out_dir, ctx):
    """Speed one real-video file up. Returns a process-style exit code."""
    input_path = Path(input_path)
    if not input_path.is_file():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        return 1
    if not has_real_video(input_path):
        print(f"Error: {input_path.name} has no video stream "
              "(use an audio format for audio-only files)", file=sys.stderr)
        return 1

    token = ctx.get("format_token") or args.format
    if isinstance(token, (list, tuple)):
        token = token[0] if token else "flac"
    parsed = parse_video_format(str(token))
    if parsed is None:
        print(f"Error: unknown video format {token!r} "
              "(use a preset mp4/mkv/mp4alac/webm or a pair h264/aac)",
              file=sys.stderr)
        return 1
    vcodec, acodec, container_key, ext = parsed
    video_enc = VIDEO_ENCODERS.get(vcodec)
    if video_enc is None:
        print(f"Error: unknown video codec {vcodec!r}", file=sys.stderr)
        return 1
    if not ffmpeg_has_encoder(video_enc):
        print(f"Error: this ffmpeg lacks the {vcodec} encoder ({video_enc})",
              file=sys.stderr)
        return 127

    # Audio backend (aac/opus lossy picking mirrors -F aac/-F opus).
    lossy_opts = ctx.get("lossy_opts") or []
    backend = ctx.get("backend")
    is_lossy_audio = acodec in ("aac", "opus", "mp3")
    if is_lossy_audio and backend is None and lossy_opts is not None:
        # cli resolves lossy backends for audio tokens; pair tokens need
        # their own resolution here.
        dialect = None
        try:
            kind = "aac" if acodec == "aac" else acodec
            dialect = detect_lossy_dialect(kind, lossy_opts)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        backend = select_lossy_backend(
            "aac" if acodec == "aac" else acodec, dialect)
        if backend is None:
            return 127
        if not lossy_opts:
            if acodec == "aac" and backend.get("kind") == "ffmpeg" \
                    and backend.get("encoder") == "libfdk_aac":
                default = ["-vbr", "5"]
            else:
                default = LOSS_DEFAULTS.get((acodec, backend.get("kind")))
            if default is None:
                print("Error: ffmpeg AAC has no default (it isn't that "
                      "great); pass explicit --lossy-opts, e.g. "
                      "--lossy-opts=-b:a 160k (or better: install qaac)",
                      file=sys.stderr)
                return 1
            lossy_opts = list(default)
            say(f"Default lossy opts for {acodec} via "
                f"{backend['kind']}: {' '.join(default)}")
    use_qaac = (is_lossy_audio and backend is not None
                and backend.get("kind") == "qaac" and acodec == "aac")

    try:
        duration_factor, pitch_extra, sample_rate = _speedup_plan(args, input_path)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    probe = probe_audio(input_path)
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

    # Filename: --autorename renders, otherwise stem [Nx].
    _artist = get_artist(input_path)
    _orig_title = get_title(input_path, fallback=input_path.stem)
    _diff_bare = (f"{duration_factor:g}x" if duration_factor != 1.0 else "")
    _auto = getattr(args, "autorename", None)
    _rendered = None
    if _auto is not None:
        if _auto == "":
            from .tags import autorename_base
            _base_t = _orig_title
            # Stripped title for legacy mode mirrors audio naming.
            _rendered = autorename_base(
                _artist, _base_t, input_path.stem,
                drop_shift=bool(_diff_bare), drop_rate=False)
            if _diff_bare:
                _rendered = f"{_rendered} [{_diff_bare}]"
        else:
            _rendered = render_autorename(
                args, _artist, _orig_title, input_path.stem,
                diff=_diff_bare)
    parent = (out_dir if out_dir is not None
              else single_parent(args, input_path))
    if args.output is not None and out_dir is None:
        output_path = parent / args.output
    elif _rendered:
        output_path = parent / (sanitize_filename(_rendered) + ext)
    else:
        suffix = (f" [{duration_factor:g}x]"
                  if duration_factor != 1.0 else "")
        output_path = parent / (sanitize_filename(
            input_path.stem + suffix) + ext)
    if output_path == input_path:
        print("Error: output would overwrite input, aborting", file=sys.stderr)
        return 1
    if output_path.exists() and not args.overwrite:
        print(f"Error: output already exists: {output_path} (use -y to overwrite)",
              file=sys.stderr)
        return 1
    if args.dry_run:
        say(f"{output_path.name}: would speed {input_path.name} "
            f"{duration_factor:g}x -> {container_key} "
            f"({vcodec} + {acodec})")
        return 0

    tempdir = None
    try:
        aac_tmp = None
        if use_qaac and n_audio:
            tempdir = Path(tempfile.mkdtemp(prefix="nightcore-video-"))
            _codec, _br, _bits, _fbr, _enc = get_audio_info(input_path)
            depth_pcm = wav_codec_for(_bits)
            tmp_wav = tempdir / "video.wav"
            aac_tmp = tempdir / "video-aac.m4a"
            say(f"  ffmpeg decode -> {depth_pcm} (sped {duration_factor:g}x)")
            decode_cmd = ["ffmpeg", "-nostdin", "-y", "-i", str(input_path),
                          "-map", "0:a", "-c:a", depth_pcm]
            if audio_chain:
                # CoreAudio rejects the sped (non-standard) rate, so the
                # temp WAV goes back to the source rate. Duration and
                # pitch survive: aresample preserves both.
                chain = audio_chain
                if sample_rate:
                    chain += f",aresample={sample_rate}"
                decode_cmd += ["-af", chain]
            decode_cmd += [str(tmp_wav)]
            run_tool(decode_cmd)
            say(f"  qaac encode aac ({' '.join(lossy_opts)})")
            run_qaac_alac(tmp_wav, aac_tmp, alac=False, extra_args=lossy_opts)
            try:
                tmp_wav.unlink()
            except OSError:
                pass

        v_parts = "format=yuv420p"
        fc = []
        if duration_factor != 1.0:
            fc.append(f"[0:v:0]setpts=PTS/{duration_factor:g},{v_parts}[vout]")
        else:
            fc.append(f"[0:v:0]{v_parts}[vout]")
        cmd = ["ffmpeg", "-nostdin",
               "-y" if args.overwrite else "-n",
               "-i", str(input_path)]
        if aac_tmp is not None:
            cmd += ["-i", str(aac_tmp)]
        cover_flag = ctx.get("cover_flag")
        cover_idx = None
        if cover_flag is not None and container_key != "webm":
            cover_idx = 1 + (1 if aac_tmp is not None else 0)
            cmd += ["-i", str(cover_flag)]
        audio_labels = []
        for i in range(n_audio):
            if aac_tmp is not None:
                # qaac output is stream-copied in the mux; it must be
                # mapped directly (ffmpeg refuses streamcopy of a
                # filtergraph-fed stream).
                continue
            label = f"aout{i}"
            audio_labels.append(label)
            if audio_chain:
                fc.append(f"[0:a:{i}]{audio_chain}[{label}]")
            else:
                fc.append(f"[0:a:{i}]anull[{label}]")
        cmd += ["-filter_complex", ";".join(fc)]
        cmd += ["-map", "[vout]"]
        for label in audio_labels:
            cmd += ["-map", f"[{label}]"]
        if aac_tmp is not None:
            cmd += ["-map", "1:a:0"]
        if cover_idx is not None:
            cmd += ["-map", f"{cover_idx}:v"]
        for i in range(n_subs):
            cmd += ["-map", f"0:s:{i}"]
        # Per-encoder quality: h264 takes preset+crf, svt-av1 takes
        # numeric preset+crf, vpx encoders want -b:v 0 with crf.
        if vcodec in ("h264", "avc", "hevc", "h265"):
            cmd += ["-c:v", video_enc, "-preset", MKVID_PRESET, "-crf",
                    MKVID_CRF, "-pix_fmt", "yuv420p"]
        elif vcodec == "av1":
            cmd += ["-c:v", video_enc, "-preset", "6", "-crf", "28",
                    "-pix_fmt", "yuv420p"]
        else:  # vp9 / vp8
            cmd += ["-c:v", video_enc, "-b:v", "0", "-crf", "30",
                    "-deadline", "good", "-pix_fmt", "yuv420p"]
        if aac_tmp is not None:
            cmd += ["-c:a", "copy"]
        elif acodec == "aac" and backend and backend.get("kind") == "ffmpeg":
            enc = backend.get("encoder") or "aac"
            cmd += ["-c:a", enc] + list(lossy_opts)
        elif is_lossy_audio:
            cmd += ["-c:a", {"aac": "aac", "opus": "libopus",
                              "mp3": "libmp3lame"}.get(acodec, acodec)] \
                + list(lossy_opts)
        else:
            cmd += ["-c:a", acodec]
        if n_subs:
            # Embedded subtitles: keep ass in mkv (styling preserved),
            # convert elsewhere (mp4 cannot carry ass, webm wants webvtt).
            sub_streams = (probe.get("subtitles") or [])
            has_ass = any(
                str(s.get("codec_name") or "").lower() in ("ass", "ssa")
                for s in sub_streams)
            if has_ass and container_key == "mkv":
                sub_codec = "ass"
            else:
                sub_map = {"mp4": "mov_text", "mp4alac": "mov_text",
                           "mkv": "srt", "webm": "webvtt"}
                sub_codec = sub_map.get(container_key, "mov_text")
            cmd += ["-c:s", sub_codec]
            if duration_factor != 1.0:
                cmd += ["-bsf:s",
                        f"setts=ts=TS/{duration_factor:g}"
                        f":duration=DURATION/{duration_factor:g}"]
        # Cover: mp4/mkv carry it as an attached picture; webm ignores.
        if cover_flag is not None and container_key == "webm":
            print("Warning: --cover ignored for webm output "
                  "(container cannot carry thumbnails)", file=sys.stderr)
        if cover_idx is not None:
            cmd += ["-c:v:1", "mjpeg", "-disposition:v:1", "attached_pic"]
        if ext == ".mp4":
            cmd += ["-movflags", "+faststart"]
        cmd += ["-map_metadata", "0"]
        # Tags: template (--tag-template) wins, else source pass-through.
        tmpl = ctx.get("tmpl")
        _diff = (f"{duration_factor:g}x" if duration_factor != 1.0 else "")
        if tmpl is not None:
            (title_base, ff_extra, stub_encoder, eff_artist,
             original_title, title_verbatim) = apply_template_tags(
                tmpl, input_path, _orig_title, _artist,
                keep_missing=bool(getattr(args, "no_delete_tags", False)),
                slots={"diff": _diff, "quality": "", "resample": "",
                       "denightcore": ""})
            from .naming import clean_source_title
            if title_verbatim:
                title = title_base
            else:
                title = compute_new_title(
                    tmpl, title_base, clean_source_title(_orig_title),
                    "", diff=_diff)
            tags = read_tags(input_path, "title", "artist", "album", "date",
                             "genre", "comment", "description")
            if not getattr(args, "no_delete_tags", False):
                # strip mode: template only (plus title/artist above)
                merged = {**ff_extra}
                # apply_template_tags already stripped and aliased.
                tags = {**tags, **merged}
                for _k in list(tags):
                    if _k not in merged and _k not in ("title", "artist"):
                        tags.pop(_k, None)
            else:
                tags = {**tags, **ff_extra}
            title = title or (ff_extra.get("title") or title_base
                              or _orig_title or input_path.stem)
            artist = eff_artist or ff_extra.get("artist")
        else:
            tags = read_tags(input_path, "title", "artist", "album", "date",
                             "genre", "comment", "description")
            _src_title = tags.get("title") or _orig_title or input_path.stem
            if _diff and _src_title:
                title = f"{_src_title} [Nightcore {_diff}]"
            else:
                title = _src_title
            artist = tags.get("artist")
        cmd += ["-metadata", f"title={title}"]
        if artist:
            cmd += ["-metadata", f"artist={artist}"]
        for key in ("album", "date", "genre", "comment", "description"):
            value = tags.get(key)
            if value and key not in ("title", "artist"):
                cmd += ["-metadata", f"{key}={value}"]
        # Extra template keys (everything else in the template file).
        if tmpl is not None:
            for _k, _v in (ff_extra or {}).items():
                if _k not in ("title", "artist", "album", "date", "genre",
                              "comment", "description") and _v:
                    cmd += ["-metadata", f"{_k}={_v}"]
        if not ctx.get("no_stamp"):
            cmd += ["-metadata",
                    f"description={tool_stamp()} --video {duration_factor:g}x"]
        cmd += [str(output_path)]
        say(f"{output_path.name}: speeding {duration_factor:g}x "
            f"-> {container_key} ({vcodec} + {acodec})")
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
