"""Output naming that needs probe/tag data."""

from pathlib import Path
import re
import sys

from .audio import format_rate, get_audio_info, get_sample_rate, mp3_header_info, next_standard_rate, packet_vbr_info, semitones_from_speed, speed_from_semitones, wav_codec_for
from .constants import LOSSY_CODECS, OUTPUT_FORMATS
from .naming import clean_source_title, lossy_suffix_for, strip_denightcore_tags, strip_quality_rate_tags
from .tags import apply_template_tags, clean_encoder, get_artist, get_title, render_autorename, sanitize_filename

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
    forward_codec = re.search(r"source-codec=(\S+)", stamp_text)
    forward_lossy = (forward_codec is not None
                     and forward_codec.group(1).lower() in LOSSY_CODECS)
    if (d_st != 0.0 or target_rate is not None or is_lossy_output
            or (codec or "") in LOSSY_CODECS or forward_lossy
            or "resample=" in stamp_text):
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
    elif codec == "opus":
        # Opus has true CBR and VBR modes but stores no flag for them.
        mode = packet_vbr_info(input_path)
    # Vorbis is variable by design and AAC exposes no reliable in-band
    # mode signal, so neither gets a VBR/CBR label.
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
        # In pitch mode the label is semitones; --semitones input is reused
        # verbatim, otherwise derived from the speed factor. Combo jobs
        # label both parts: [1.2x 2.166866948st].
        st_label = (st_signed if st_signed is not None
                    else semitones_from_speed(speed))
        # Full precision: negating the label value inverts the shift exactly.
        if combo:
            mode_suffix = f" [{speed!r}x {st_label!r}st]"
        else:
            mode_suffix = f" [{st_label!r}st]"
    else:
        mode_suffix = "" if speed == 1.0 else f" [{speed!r}x]"
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
    # mode_suffix looks like " [1.2x]" / " [1.2x 2.16st]" / ""; strip the
    # outer brackets for the bare diff.
    if mode_suffix.startswith(" [") and mode_suffix.endswith("]"):
        diff_bare = mode_suffix[2:-1]
    else:
        diff_bare = ""
    if middle_suffix.startswith(" [") and middle_suffix.endswith("]"):
        quality_bare = middle_suffix[2:-1]
    else:
        quality_bare = ""
    if rate_suffix.startswith(" [") and rate_suffix.endswith("]"):
        resample_bare = rate_suffix[2:-1]
    else:
        resample_bare = ""
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
    weird_suffix = mode_suffix + middle_suffix
    standard_suffix = weird_suffix + rate_suffix
    if args.denightcore and denight is not None:
        # -D names are rebuilt from tags in run_conversion (Artist - Title
        # [quality] [denightcored BP/NBP]); the suffixes below only feed
        # lyrics mirroring. {resample} still reports a fresh rate tag.
        weird_suffix, standard_suffix = denight_suffix, \
            denight_suffix + rate_suffix

    out_ext = OUTPUT_FORMATS[args.format]["ext"]
    out_codec = OUTPUT_FORMATS[args.format]["codec"] or wav_codec_for(bits)
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
    # Fresh encodes retitle from the source tag, so stale script groups
    # go too (a re-encode would otherwise duplicate them next to the
    # fresh tags). Explicit template titles bypass all of this verbatim.
    # (-D restores instead: run_conversion cleans its title separately.)
    original_title = strip_denightcore_tags(original_title)
    if denight is None:
        original_title = clean_source_title(original_title)

    # Tag template: override/keep source tags, strip everything else at
    # encode time. A title=/artist= value may use {slots} (rendered here
    # from the per-job facts); any explicit title= is used verbatim.
    ff_extra = {}
    title_base = original_title
    stub_encoder = None
    title_verbatim = False
    if tmpl is not None:
        (title_base, ff_extra, stub_encoder, artist,
         original_title, title_verbatim) = apply_template_tags(
             tmpl, input_path, original_title, artist,
             keep_missing=args.no_delete_tags,
             slots={"diff": diff_bare, "quality": quality_bare,
                    "resample": resample_bare,
                    "denightcore": denightcore_bare})

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
        "title_base": title_base,
        "title_verbatim": title_verbatim,
        "ff_extra": ff_extra,
        "stub_encoder": stub_encoder,
    }


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
        name_base = strip_denightcore_tags(Path(audio_path).stem)
    elif args.autorename == "":
        # Bare --autorename: legacy "Artist - Title" + full suffix.
        if naming["artist"]:
            name_base = f"{naming['artist']} - {naming['original_title']}"
        else:
            name_base = naming["original_title"]
    else:
        # Custom --autorename TEMPLATE already carries its own
        # {diff}/{quality}/{resample} slots: no suffix appended.
        custom = render_autorename(
            args, naming["artist"], naming["original_title"],
            strip_denightcore_tags(Path(audio_path).stem),
            diff=naming["diff_bare"],
            quality=naming["quality_bare"],
            resample=naming["resample_bare"],
            denightcore=naming["denightcore_bare"])
        return sanitize_filename(custom)
    return sanitize_filename(name_base + suffix)


