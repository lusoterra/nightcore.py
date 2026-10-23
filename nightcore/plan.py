"""Output naming that needs probe/tag data."""

from pathlib import Path
import re
import sys

from .audio import format_rate, get_audio_info, get_sample_rate, mp3_header_info, next_standard_rate, packet_vbr_info, semitones_from_speed, speed_from_semitones, wav_codec_for
from .constants import LOSSY_CODECS, OUTPUT_FORMATS
from .naming import clean_source_title, lossy_suffix_for, split_shift_tags, strip_denightcore_tags, strip_empty_display_tags, strip_quality_rate_tags
from .tags import apply_template_tags, autorename_base, clean_encoder, display_suffix, fold_album_artist, get_artist, get_title, pipeline_has, pipeline_origin, read_shift_source, render_autorename, round_diff_bare, sanitize_filename

def denightcore_verdict(d_st, target_rate, is_lossy_output, codec,
                        stamp_text=""):
    """"denightcored BP" or "denightcored NBP" for a -D undo.

    Bit-perfect means a pure sample-rate inversion, nothing else: any
    pitch resynthesis (rubberband), any resampling -- now (target_rate)
    or in the forward job (a resample= marker in the input's stamp) --
    or any lossy stage makes it NBP. Lossy origin hides in FLAC wrappers
    (probe sees flac), so the input stamp's source-codec= is consulted
    too, because the filename is untrusted.
    """
    stamp_text = stamp_text or ""
    origin = pipeline_origin(stamp_text)
    forward_lossy = (origin["codec"] or "").lower() in LOSSY_CODECS
    if (d_st != 0.0 or target_rate is not None or is_lossy_output
            or (codec or "") in LOSSY_CODECS or forward_lossy
            or pipeline_has(stamp_text, "resample")):
        return "denightcored NBP"
    return "denightcored BP"


def resolve_audio_naming(args, tmpl, input_path, speed,
                           combo, st_signed, combo_seq, eff_semitones,
                           denight=None, quiet=False):
    """Compute everything the output filename needs, without converting.

    denight is None, or dict(d_speed, d_st, d_seq, stamp_text) with the
    forward shift recovered from the input's stamp in -D mode. Returns a
    dict with speed, sample_rate, target_rate, is_lossy_output, codec
    bits info, pitch_mode, pitch_rb, weird/standard suffixes, bare slot
    components, out_ext/codec/depths, artist, original_title, title_base,
    title_verbatim, ff_extra and stub_encoder -- or None when the sample
    rate cannot be probed (caller reports it).
    Warnings (-R ignored/exceeds) print unless quiet (lyrics mirroring
    predicts names without nagging twice).
    """
    if denight is not None:
        d_speed = denight["d_speed"]
        d_st = denight["d_st"]
        d_seq = denight["d_seq"]
        speed = 1.0 / d_speed
    else:
        d_st = 0.0
        d_seq = 1.0

    sample_rate = get_sample_rate(input_path)
    if sample_rate is None:
        return None

    # Decide resample target. Default: no resampling (bit-perfect weird rate).
    # In -D mode the undo itself changes the rate, so select from that.
    # Lossy outputs always keep the weird rate: main rejects -R with
    # lossy output loudly, so the encoder gets the untouched rate, and
    # if it chokes on it, it fails loudly instead of silently
    # resampling. (The -R warning below is unreachable via main.)
    is_lossy_output = bool(OUTPUT_FORMATS[args.format].get("lossy"))
    target_rate = None
    weird_rate = sample_rate * speed * combo_seq / d_seq
    if is_lossy_output and args.resample:
        if not quiet:
            print("Warning: -R ignored for lossy output "
                  "(weird rate passed through bit-perfect)", file=sys.stderr)
    elif args.resample:
        target_rate = next_standard_rate(weird_rate)
        if target_rate is None:
            if not quiet:
                print(
                    f"Warning: effective rate {weird_rate:.0f} Hz exceeds "
                    f"highest known standard rate; keeping weird rate.",
                    file=sys.stderr,
                )
        elif target_rate == round(weird_rate):
            # Already a standard rate: resampling would be a no-op.
            target_rate = None

    # Build filename/title suffix:
    # "[<speed>x|X.XXst] [<codec> <bitrate>k] [<rate>]"
    # - speed/st omitted when amount is 1 (0st)
    # - lossy tag added when the source codec is lossy (to flag that the
    #   output is a lossless container around a lossy source)
    # - rate appended on resample.
    codec, bit_rate, bits, format_bit_rate, raw_encoder = get_audio_info(input_path)
    mode, encoder = None, clean_encoder(raw_encoder, codec)
    if codec == "mp3":
        mode, lame = mp3_header_info(input_path)
        encoder = lame or encoder
        if mode == "VBR" and format_bit_rate:
            # Stream bit_rate on VBR MP3s is unreliable; use file average.
            bit_rate = format_bit_rate
    elif codec in ("aac", "opus"):
        # Opus has true CBR and VBR modes but stores no flag for them.
        # AAC exposes no in-band mode flag either, so it shares the
        # packet scan (best available signal; AAC CBR packets still
        # vary, so most AAC reads VBR).
        mode = packet_vbr_info(input_path)
    # Vorbis is variable by design, so it gets no VBR/CBR label.
    lossy_suffix = lossy_suffix_for(codec, bit_rate, mode, encoder)
    if lossy_suffix:
        # e.g. [mp3 CBR 320k LAME3.100]: documents a lossy origin.
        middle_suffix = lossy_suffix
    elif bits:
        # e.g. [24bit 44.1kHz]: documents lossless source depth/rate,
        # which is what you need to revert to original speed.
        middle_suffix = f" [{bits}bit {format_rate(sample_rate)}]"
    else:
        # Float/unknown depth (e.g. 32-bit float masters): rate only.
        middle_suffix = f" [{format_rate(sample_rate)}]"
    # -S/--semitones implies pitch-only; -p is the -s spelling of the same.
    # Alongside -S, -p is completely ignored (same amount either way).
    # -D inverts the stamp-recorded shift: pitch is needed whenever the
    # recorded st != 0.
    pitch_mode = ((args.pitch or eff_semitones is not None) and speed != 1.0
                  or (args.denightcore and d_st != 0.0))
    if pitch_mode:
        # In pitch mode the shift is semitones; --semitones input is reused
        # verbatim, otherwise derived from the speed factor. Full precision
        # here: diff_bare feeds audio math, -D inversion and template
        # slots, while mode_suffix below renders the display form.
        st_label = (st_signed if st_signed is not None
                    else semitones_from_speed(speed))
        if combo:
            diff_bare = f"{speed!r}x {st_label!r}st"
        else:
            diff_bare = f"{st_label!r}st"
    else:
        diff_bare = "" if speed == 1.0 else f"{speed!r}x"
    # Display form for filenames ("[Nightcore 1.2x 2.17st]"): rounded to
    # 2 decimals like titles. Quality never rides filenames (pipeline +
    # explicit template {quality} only).
    mode_suffix = display_suffix(round_diff_bare(diff_bare, 2))
    rate_suffix = f" [{format_rate(target_rate)}]" if target_rate is not None else ""
    # No rubberband extras exist (R3 defaults do everything): pitch jobs
    # run bare. split_shift_tags still strips legacy [rb ...] groups from
    # names so old files denightcore cleanly.
    pitch_rb = []
    if is_lossy_output:        # Lossy sharing files carry only the shift amount: no source-quality
        # tag (stale or misleading once re-encoded) and no rate tag (the
        # rate is always a compatible standard one by construction).
        middle_suffix = ""
        rate_suffix = ""
    # Bare components for --autorename/{diff}/{quality}/
    # {resample} templates (no brackets; the template supplies them).
    # diff_bare is already canonical full-precision (built above); only
    # mode_suffix carries the display form.
    if middle_suffix.startswith(" [") and middle_suffix.endswith("]"):
        quality_bare = middle_suffix[2:-1]
    else:
        quality_bare = ""
    if rate_suffix.startswith(" [") and rate_suffix.endswith("]"):
        resample_bare = rate_suffix[2:-1]
    else:
        resample_bare = ""
    # Identity jobs inherit the source record: a speed-1.0 transcode (or
    # distribution encode) applies no shift of its own, so the file's true
    # total shift carries on into names, titles, template slots and the
    # pipeline instead of a blank 1.0x erasing history. Full precision
    # (the record is machine data); display rounding happens downstream.
    inherited = None
    if not diff_bare and denight is None:
        _parsed, _ = read_shift_source(input_path)
        if _parsed is not None and (_parsed[0] != 1.0 or _parsed[1] != 0.0):
            inherited = _parsed
            _sp, _st = inherited
            diff_bare = ((f"{_sp!r}x" if _sp != 1.0 else "")
                         + (" " if _sp != 1.0 and _st != 0.0 else "")
                         + (f"{_st!r}st" if _st != 0.0 else ""))
            mode_suffix = display_suffix(round_diff_bare(diff_bare, 2))
    denightcore_bare = ""
    denight_suffix = ""
    if args.denightcore and denight is not None:
        # Bit-perfect verdict for the undo (mirrors the convert-time
        # warning): pure sample-rate inversion is BP, anything else NBP.
        # The input stamp is consulted, not the filename: it records
        # whether the forward job itself resampled.
        denightcore_bare = denightcore_verdict(
            d_st, target_rate, is_lossy_output, codec,
            denight.get("stamp_text", ""))
        denight_suffix = f" [{denightcore_bare}]"
        # Templates for -D see no forward shift: the undo removes it, so
        # {diff} is empty and {denightcore} carries the verdict instead.
        # {quality} is rebuilt for the RESTORED audio: probing the input
        # sees the sped-up rate (e.g. 52920 Hz), but the output returns
        # to rate / d_speed (/ pitch factor), i.e. the OG fingerprint
        # in the BP case -- which is what the name must document.
        if target_rate is not None:
            restored_rate = target_rate
        else:
            restored_rate = round(
                sample_rate / d_speed / speed_from_semitones(d_st))
        if lossy_suffix:
            middle_suffix = lossy_suffix
        elif bits:
            middle_suffix = f" [{bits}bit {format_rate(restored_rate)}]"
        else:
            middle_suffix = f" [{format_rate(restored_rate)}]"
        if middle_suffix.startswith(" [") and middle_suffix.endswith("]"):
            quality_bare = middle_suffix[2:-1]
        else:
            quality_bare = ""
        diff_bare = ""
    weird_suffix = mode_suffix
    standard_suffix = mode_suffix + rate_suffix
    if args.denightcore and denight is not None:
        # -D names are rebuilt from tags in run_conversion (Artist - Title
        # [quality] [denightcored BP/NBP]); the suffixes below only feed
        # lyrics mirroring. {resample} still reports a fresh rate tag.
        weird_suffix, standard_suffix = denight_suffix, \
            denight_suffix + rate_suffix

    out_ext = OUTPUT_FORMATS[args.format]["ext"]
    out_codec = OUTPUT_FORMATS[args.format]["codec"]
    # Collapse sub-16-bit/unknown (lossy, float) sources to 16-bit output:
    # an MP3 carries no more real detail, and float temps would otherwise
    # bloat finals to 32-bit. Genuine 24/32-bit sources keep their depth.
    depth_fmt = None if (bits is not None and bits > 16) else "s16"
    depth_pcm = wav_codec_for(bits)

    stem = input_path.stem
    original_title = get_title(input_path, fallback=stem)
    artist = get_artist(input_path)
    # Past verdicts go stale on new audio work: drop them here so titles,
    # template {title} slots and bare-autorename bases never accumulate
    # "[denightcored BP] [1.3x] [denightcored BP] ..." across cycles.
    # Fresh encodes retitle from the source tag: stale shift groups go
    # only where fresh tags replace them (a speed-1.0 transcode of a
    # [1.1x] file keeps describing it), quality groups always (titles
    # never carry quality; filenames do). Explicit template titles
    # bypass all of this verbatim.
    # (-D restores instead: run_conversion cleans its title separately.)
    original_title = strip_empty_display_tags(
        strip_denightcore_tags(original_title))
    if denight is None:
        if diff_bare:
            original_title = split_shift_tags(original_title)[0]
        original_title = strip_quality_rate_tags(original_title)

    # Tag template: override/keep source tags, strip everything else at
    # encode time. A title=/artist= value may use {slots} (rendered here
    # from the per-job facts); any explicit title= is used verbatim.
    # Filenames keep the source record: --autorename {title} renders the
    # source title, never the template's (a template "My Mix" must not
    # rename every file alike, and a template "[Nightcore {diff}]"
    # must not double the filename's own [Nightcore {diff}]).
    ff_extra = {}
    title_base = original_title
    stub_encoder = None
    title_verbatim = False
    source_title = original_title
    source_artist = artist
    if tmpl is not None:
        (title_base, ff_extra, stub_encoder, artist,
         original_title, title_verbatim) = apply_template_tags(
             tmpl, input_path, original_title, artist,
             keep_missing=args.no_delete_tags,
             slots={"diff": diff_bare, "quality": quality_bare,
                    "resample": resample_bare,
                    "denightcore": denightcore_bare})
        if out_ext != ".m4a":
            # Canonical albumartist everywhere except movenc, which drops
            # the bare spelling (hence the alias above).
            ff_extra = fold_album_artist(ff_extra)

    if is_lossy_output:
        # Lossy sharing files carry no source-quality or rate tags: strip
        # them from the inherited title/filename base as well (an old
        # "[16bit 44.1kHz]" riding along in the source title must not
        # survive the transcode). Shift tags stay: only quality/rate go.
        original_title = strip_quality_rate_tags(original_title)

    return {
        "speed": speed,
        "sample_rate": sample_rate,
        "target_rate": target_rate,
        "is_lossy_output": is_lossy_output,
        "bits": bits,
        "codec": codec,
        "bit_rate": bit_rate,
        "mode": mode,
        "encoder": encoder,
        "pitch_mode": pitch_mode,
        "pitch_rb": pitch_rb,
        "weird_suffix": weird_suffix,
        "standard_suffix": standard_suffix,
        "diff_bare": diff_bare,
        "quality_bare": quality_bare,
        "resample_bare": resample_bare,
        "denightcore_bare": denightcore_bare,
        "out_ext": out_ext,
        "out_codec": out_codec,
        "depth_fmt": depth_fmt,
        "depth_pcm": depth_pcm,
        "artist": artist,
        "original_title": original_title,
        "source_title": source_title,
        "source_artist": source_artist,
        "title_base": title_base,
        "title_verbatim": title_verbatim,
        "inherited": inherited,
        "ff_extra": ff_extra,
        "stub_encoder": stub_encoder,
    }


def stage_records(jobs, *, sample_rate, speed, use_qaac, qaac_dither,
                    cli_backend, is_lossy_output, enc_fmt):
    """Per-tool stage arguments for the pipeline record, by job plan.

    Mirrors the executor in run_conversion (prescale/settle skip rules
    must stay in sync with it): ffmpeg audio-filter chains, sox rate
    targets, rubberband flags, and a gain=auto marker wherever the
    anti-clip trim is armed (pitch jobs; it fires only on hot masters,
    measured at runtime). Values carry no spaces (filter chains use
    ffmpeg's colon syntax). Returns {tool: [stage, ...]}; tools with no
    stages render bare.
    """
    stages = {"ffmpeg": [], "sox": [], "rubberband": []}
    for _, plan in jobs:
        kind = plan[0]
        if kind == "direct":
            if plan[1] is not None:
                stages["ffmpeg"].append(f"af={plan[1]}")
        elif kind == "resample":
            _, prefilter, target = plan
            if prefilter is not None:
                stages["ffmpeg"].append(f"af={prefilter}")
            stages["sox"].append(f"rate={target}Hz")
        elif kind == "pitch":
            _, prefilter, pitched_rate, pitch_st, target, rb = plan
            if prefilter is not None:
                stages["ffmpeg"].append(f"af={prefilter}")
            cur_rate = (round(sample_rate * speed)
                        if prefilter is not None else sample_rate)
            if pitch_st >= 0:
                prescale_to = (target if target is not None
                               else pitched_rate)
                if prescale_to != cur_rate:
                    stages["sox"].append(f"rate={prescale_to}Hz")
            stages["rubberband"].append(
                "-3,-p=" + repr(float(pitch_st))
                + "".join("," + str(e).replace(" ", "=") for e in rb))
            stages["sox"].append("gain=auto")
            settle = (target if target is not None
                      else (pitched_rate if pitch_st < 0 else None))
            if settle is not None and settle != cur_rate:
                stages["sox"].append(f"rate={settle}Hz")
    # Dither on processed 16-bit final encodes (mirrors finalize_wav and
    # the qaac float-int conversion; direct paths stay bit-perfect).
    dither = None
    if use_qaac:
        dither = qaac_dither
    elif (not cli_backend
            and any(plan[0] != "direct" for _, plan in jobs)
            and not is_lossy_output and enc_fmt == "s16"):
        dither = enc_fmt
    if dither is not None:
        stages["ffmpeg"].append(
            "dither=aresample=dither_method=triangular:"
            f"out_sample_fmt={dither}")
    return stages


def predict_audio_stem(args, ctx, audio_path):
    """Predict the output filename stem run_conversion would use.

    Used by .lrc lyrics jobs so they follow their parent audio file
    with the same name. Returns the sanitized stem (no extension), or
    None when it cannot be determined (unreadable file, unknown rate).
    Picks the resampled variant under -R, like the compatible file
    a player would actually pair with. Never prints, never raises.
    """
    if audio_path is None:
        return None
    try:
        naming = resolve_audio_naming(
            args, ctx["tmpl"], Path(audio_path),
            ctx["speed"], ctx["combo"], ctx["st_signed"],
            ctx["combo_seq"], ctx["eff_semitones"],
            denight=None, quiet=True)
    except (OSError, ValueError):
        return None
    if naming is None:
        return None
    suffix = (naming["standard_suffix"]
              if naming["target_rate"] is not None
              else naming["weird_suffix"])
    if args.denightcore:
        # Lyrics retiming cannot undo anything: -D is rejected in run_lrc
        # before mirroring, so this branch is unreachable.
        return None
    elif getattr(args, "autorename", None) is None:
        name_base = clean_source_title(Path(audio_path).stem)
    elif args.autorename == "":
        # Empty --autorename= : legacy "Artist - Title" + full suffix
        # (script groups leave the base only where the suffix re-adds
        # them -- see autorename_base).
        name_base = autorename_base(
            naming["artist"], naming["original_title"],
            strip_denightcore_tags(Path(audio_path).stem),
            drop_shift=bool(naming["diff_bare"]),
            drop_rate=bool(naming["resample_bare"]))
    else:
        # Custom --autorename TEMPLATE already carries its own
        # {diff}/{quality}/{resample} slots: no suffix appended.
        # {title} is the source title, never the tag template's.
        custom = render_autorename(
            args, naming["artist"], naming["source_title"],
            strip_empty_display_tags(
                strip_denightcore_tags(Path(audio_path).stem)),
            diff=naming["diff_bare"],
            quality=naming["quality_bare"],
            resample=naming["resample_bare"],
            denightcore=naming["denightcore_bare"])
        return sanitize_filename(custom)
    return sanitize_filename(name_base + suffix)


def _fast_inherited_shift(input_path):
    """(speed, semitones) from tags without ffprobe, or None. Never raises.

    Identity jobs (speed 1.0, no pitch) inherit the source record; the
    full path reads it via read_shift_source (native tool or ffprobe).
    The fast skip path only needs the shift itself, so Vorbis-family
    files (flac/opus/ogg) are read with mutagen in-process; anything
    else falls back to read_shift_source (cached, still cheaper than a
    full naming resolve which also probes rates and quality).
    """
    from .tags import parse_stamp_shifts
    ext = Path(input_path).suffix.lower()
    if ext in (".flac", ".opus", ".ogg", ".oga"):
        try:
            if ext == ".flac":
                from mutagen.flac import FLAC
                audio = FLAC(str(input_path))
                items = list((audio.tags or {}).items()) \
                    if getattr(audio, "tags", None) else []
            elif ext == ".opus":
                from mutagen.oggopus import OggOpus
                audio = OggOpus(str(input_path))
                items = list(audio.items())
            else:
                from mutagen.oggvorbis import OggVorbis
                audio = OggVorbis(str(input_path))
                items = list(audio.items())
        except Exception:
            return None
        texts = []
        for want in ("encoder_settings", "description"):
            for k, v in items:
                if str(k).lower() == want:
                    if isinstance(v, (list, tuple)):
                        v = v[0] if v else ""
                    texts.append(str(v))
                    break
        for text in texts:
            try:
                parsed = parse_stamp_shifts(text)
            except Exception:
                parsed = None
            if parsed is not None:
                return parsed
        return None
    try:
        parsed, _ = read_shift_source(input_path)
    except Exception:
        return None
    return parsed


def fast_default_stem(args, ctx, audio_path):
    """Sanitized output stem for default-named batch jobs, or None.

    No ffprobe, no packet scan: default names are clean(stem) +
    display(diff), where diff comes from the job settings alone except
    for speed-1.0 identity jobs, which inherit the source record (read
    via _fast_inherited_shift). Quality/rate probes never feed default
    filenames (they only fill --autorename slots), so lossless jobs are
    equally predictable. Returns None when the job is not eligible
    (denightcore, custom autorename, resample) so callers fall back to
    the full resolve.
    """
    from .audio import semitones_from_speed
    from .tags import display_suffix, round_diff_bare
    if args.denightcore:
        return None
    if getattr(args, "autorename", None) is not None:
        return None
    if getattr(args, "resample", False):
        return None
    try:
        speed = ctx["speed"]
        combo = ctx["combo"]
        st_signed = ctx["st_signed"]
        eff_semitones = ctx["eff_semitones"]
    except KeyError:
        return None
    pitch_mode = ((args.pitch or eff_semitones is not None)
                  and speed != 1.0)
    if pitch_mode:
        if combo:
            diff_bare = f"{speed!r}x {st_signed!r}st"
        else:
            st_label = (st_signed if st_signed is not None
                        else semitones_from_speed(speed))
            diff_bare = f"{st_label!r}st"
    elif speed != 1.0:
        diff_bare = f"{speed!r}x"
    else:
        inherited = _fast_inherited_shift(audio_path)
        if inherited is None or (inherited[0] == 1.0
                                 and inherited[1] == 0.0):
            diff_bare = ""
        else:
            _sp, _st = inherited
            diff_bare = ((f"{_sp!r}x" if _sp != 1.0 else "")
                         + (" " if _sp != 1.0 and _st != 0.0 else "")
                         + (f"{_st!r}st" if _st != 0.0 else ""))
    suffix = display_suffix(round_diff_bare(diff_bare, 2))
    return sanitize_filename(clean_source_title(Path(audio_path).stem)
                             + suffix)
