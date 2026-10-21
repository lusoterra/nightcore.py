"""The speed/pitch conversion workflow."""

from pathlib import Path
from typing import Any
import re
import shutil
import subprocess
import sys
import tempfile

from .audio import cover_codec_name, extract_cover_tmp, format_rate, sample_is_float, semitones_from_speed, speed_from_semitones
from .backends import coreaudio_toolbox_version, find_qaac, flac_postprocess, missing_metaflac, qaac_redist_hint, run_ffmpeg, run_lame, run_opusenc, run_qaac_alac, run_rubberband, run_sox_rate, tame_hot_wav, tool_version
from .constants import COVER_CODECS, COVER_FORMATS, LOSSY_CODECS, OUTPUT_FORMATS, PIPELINE_TAG, STALE_TAGS, STAMP_TAG, _IMAGE_MIME
from .lrc import run_lrc
from .naming import claim_output_path, clean_source_title, single_parent, split_shift_tags, strip_denightcore_tags, strip_empty_display_tags, strip_quality_rate_tags
from .plan import resolve_audio_naming, stage_records
from .tags import autorename_base, build_pipeline, compute_new_title, describe_output_tags, flac_comment_wants, fold_album_artist, pipeline_has, pipeline_origin, read_all_tags, read_shift_source, render_autorename, sanitize_filename, tool_stamp
from .util import _active_outputs, batch_prefix, preview_output, say

def run_conversion(args, input_path, out_dir, ctx):
    """Convert one file. out_dir overrides the output directory (batch).

    ctx carries run-global values: tmpl, cover_flag, lossy_opts,
    backend, speed, combo, st_signed, combo_seq,
    eff_semitones.
    Returns a process-style exit code.
    """
    tmpl = ctx["tmpl"]
    cover_flag = ctx["cover_flag"]
    lossy_opts = ctx["lossy_opts"]
    backend = ctx["backend"]
    speed = ctx["speed"]
    combo = ctx["combo"]
    st_signed = ctx["st_signed"]
    combo_seq = ctx["combo_seq"]
    eff_semitones = ctx["eff_semitones"]

    input_path = Path(input_path)
    if not input_path.is_file():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        return 1

    if input_path.suffix.lower() == ".lrc":
        return run_lrc(args, input_path, out_dir, ctx)

    # -D inverts the shift recorded in the input's pipeline tag
    # (encoder_settings), falling back to a legacy DESCRIPTION Settings
    # line, instead of taking -s/-S/-p/-r. Filenames are untrusted (users
    # rename them), so they are never parsed; without a usable tag there
    # is nothing to undo.
    d_speed, d_st, d_seq = 1.0, 0.0, 1.0
    denight_raw = ""
    if args.denightcore:
        for flag, bad in (("-s/--speed", args.speed != 1.0),
                          ("-S/--semitones", eff_semitones is not None),
                          ("-p/--pitch", args.pitch),
                          ("-r/--reciprocal", args.reciprocal)):
            if bad:
                print(f"Error: --denightcore cannot be combined with {flag} "
                      "(it reads the shift from the stamp tag)", file=sys.stderr)
                return 1
        denight_raw = ""
        stamped, denight_raw = read_shift_source(input_path)
        if stamped is not None:
            d_speed, d_st = stamped
            say(f"  stamp: undoing shift "
                f"{d_speed!r}x {d_st!r}st from tags")
        if stamped is None or (d_speed == 1.0 and d_st == 0.0):
            if out_dir is not None:
                # Batch denightcore: unstamped files are skipped with a
                # tally, not failed (a mixed folder shouldn't abort).
                print(f"Skipping {input_path.name} "
                      "(magic number not found: no usable pipeline for -D)",
                      file=sys.stderr)
                ctx["skipped_notags"] = ctx.get("skipped_notags", 0) + 1
                return 0
            print(f"Error: magic number not found in '{input_path.name}'; "
                  "--denightcore restores files stamped by nightcore "
                  "(filenames alone are not trusted, renames are free)",
                  file=sys.stderr)
            return 1
        speed = 1.0 / d_speed
        d_seq = speed_from_semitones(d_st)
        if args.tag_template is not None:
            print("Warning: --tag-template ignored in denightcore mode "
                  "(restoring, not curating)", file=sys.stderr)
    naming = resolve_audio_naming(
        args, None if args.denightcore else tmpl, input_path, speed,
        combo, st_signed, combo_seq, eff_semitones,
        denight=(None if not args.denightcore else {
            "d_speed": d_speed, "d_st": d_st, "d_seq": d_seq,
            "stamp_text": denight_raw,
        }))
    if naming is None:
        print("Error: could not determine input sample rate", file=sys.stderr)
        return 1
    speed = naming["speed"]
    sample_rate = naming["sample_rate"]
    target_rate = naming["target_rate"]
    is_lossy_output = naming["is_lossy_output"]
    bits = naming["bits"]
    codec = naming["codec"]
    src_mode = naming["mode"]
    src_bit_rate = naming["bit_rate"]
    src_encoder = naming["encoder"]
    pitch_mode = naming["pitch_mode"]
    pitch_rb = naming["pitch_rb"]
    weird_suffix = naming["weird_suffix"]
    standard_suffix = naming["standard_suffix"]
    diff_bare = naming["diff_bare"]
    quality_bare = naming["quality_bare"]
    resample_bare = naming["resample_bare"]
    denightcore_bare = naming["denightcore_bare"]
    out_ext = naming["out_ext"]
    out_codec = naming["out_codec"]
    depth_fmt = naming["depth_fmt"]
    depth_pcm = naming["depth_pcm"]
    artist = naming["artist"]
    original_title = naming["original_title"]
    title_base = naming["title_base"]
    title_verbatim = naming["title_verbatim"]
    ff_extra = naming["ff_extra"]
    stub_encoder = naming["stub_encoder"]
    # Provenance stamp (--no-stamp disables): branding only, always wins
    # over a template description. The machine-readable pipeline goes to
    # PIPELINE_TAG separately (--no-pipeline disables); its tool versions
    # join later, once the backend plan is known.
    if not ctx.get("no_stamp"):
        ff_extra[STAMP_TAG] = tool_stamp()
    stamp = ff_extra.get(STAMP_TAG)
    # Tags to scrub from mapped source tags (our own marks must not ride
    # through when their writer is disabled; template-explicit values in
    # ff_extra still apply, and strip mode drops everything anyway).
    scrub_tags = []
    if ctx.get("no_stamp"):
        scrub_tags.append(STAMP_TAG)
    if ctx.get("no_pipeline"):
        scrub_tags.append(PIPELINE_TAG)
    if args.denightcore:
        # Record what was undone (forward shift) plus the verdict.
        # The denightcore= marker keeps this record from ever feeding
        # another undo (see parse_stamp_shifts). A known lossy origin
        # rides through from the input pipeline instead of re-probing
        # the wrapper container.
        _origin = pipeline_origin(denight_raw)
        pipeline_params = dict(
            speed=d_speed, semitones=d_st, pitch_mode=pitch_mode,
            target_rate=target_rate, out_format=args.format,
            lossy_opts=(), source_rate=sample_rate, source_bits=bits,
            source_codec=(_origin["codec"] or codec),
            source_mode=_origin["mode"],
            source_bitrate=_origin["bitrate"],
            source_encoder=_origin["encoder"],
            denightcore=denightcore_bare.split()[-1])
    else:
        # Forward shift in filename-tag terms ([Nx] product and [Nst]
        # sum), so -D can invert from the tags exactly like from the
        # name. NOTE: pitch-only jobs must record speed 1.0 (no rate
        # reinterpretation to undo); the internal `speed` variable
        # carries a pitch-derived factor there, which would wrongly
        # asetrate-invert on the way back.
        if combo:
            f_speed, f_st = speed, st_signed
        elif pitch_mode:
            f_speed = 1.0
            f_st = (st_signed if st_signed is not None
                    else semitones_from_speed(speed))
        else:
            f_speed, f_st = speed, 0.0
        if (f_speed == 1.0 and f_st == 0.0
                and naming["inherited"] is not None):
            # Identity transcode (format conversion, distribution encode):
            # this job applies no shift, so the source record carries on
            # instead of a blank 1.0x erasing the file's true history.
            f_speed, f_st = naming["inherited"]
        # Lossy origin fingerprint: mode/bitrate/encoder ride the
        # pipeline alongside the codec (lossless sources carry depth/rate
        # instead; a FLAC's container bitrate is content-dependent noise,
        # not a quality signal). Same rounding as lossy_suffix_for.
        src_kbps = (round(src_bit_rate / 1000)
                    if src_bit_rate is not None else 0)
        lossy_origin = codec in LOSSY_CODECS
        pipeline_params = dict(
            speed=f_speed, semitones=f_st,
            pitch_mode=(pitch_mode or f_st != 0.0),
            target_rate=target_rate,
            out_format=args.format,
            lossy_opts=(lossy_opts if is_lossy_output else ()),
            source_rate=sample_rate, source_bits=bits,
            source_codec=codec,
            source_mode=(src_mode if lossy_origin else None),
            source_bitrate=(f"{src_kbps}k"
                            if lossy_origin and src_kbps > 0 else None),
            source_encoder=(src_encoder if lossy_origin else None))

    if (not args.denightcore and not pitch_mode and speed == 1.0
            and target_rate is None
            and input_path.suffix.lower() == out_ext
            and tmpl is None and cover_flag is None
            and ctx.get("no_stamp") and ctx.get("no_pipeline")
            and getattr(args, "autorename", None) is None):
        print(
            "Error: nothing to do (speed is 1 and no resampling requested)",
            file=sys.stderr,
        )
        return 1
    # e.g. wav -> flac with speed 1: still convert to target format.

    def weird_filter():
        # Speedup is a pure rate reinterpretation: no resampling, ever.
        # (Resampling happens exclusively in SoX, only for -R outputs
        # and the pitch-subrecipe rate step.)
        if speed == 1.0:
            # Pure container/codec conversion (e.g. mp3 -> flac): no filter.
            return None
        return f"asetrate={sample_rate}*{speed}"

    # Job list: (suffix, plan); exactly one job per file. Plans:
    #   ("direct", audio_filter)                        - ffmpeg only (weird speedup)
    #   ("resample", prefilter, target)                 - ffmpeg -> sox rate -> ffmpeg
    #   ("pitch", prefilter, pitched_rate, st, target, rb) - ffmpeg -> sox rate -> rubberband -> ffmpeg
    # Temp WAVs are f32 (exact for <=24-bit int and float32 sources).
    jobs: list[tuple[str, Any]]
    if pitch_mode:
        # d_seq folds a -D pitch inversion into the same math (1.0 otherwise).
        if combo:
            pitch_st = st_signed
        else:
            pitch_st = (-d_st if args.denightcore and d_st != 0.0
                        else semitones_from_speed(speed / d_seq))
        # The SoX rate step always scales by the pitch factor from the
        # post-prefilter rate: headroom for shifted highs, exact rate.
        base_rate = (sample_rate * speed
                     if (args.denightcore or combo) else sample_rate)
        pitched_rate = round(base_rate * speed_from_semitones(pitch_st))
        # -D combined undo slows down first via asetrate; combo speeds up
        # first the same way (plain pitch jobs decode unfiltered).
        prefilter = weird_filter() if (args.denightcore or combo) else None
        if target_rate is not None:
            jobs = [(standard_suffix, ("pitch", prefilter, pitched_rate, pitch_st, target_rate, pitch_rb))]
        else:
            jobs = [(weird_suffix, ("pitch", prefilter, pitched_rate, pitch_st, None, pitch_rb))]
    elif target_rate is not None:
        jobs = [(standard_suffix, ("resample", weird_filter(), target_rate))]
    else:
        jobs = [(weird_suffix, ("direct", weird_filter()))]

    pitch_plans = [plan for _, plan in jobs if plan[0] == "pitch"]

    def _needs_sox():
        # Mirrors the executor below: SoX runs for every resample plan,
        # for pitch-up pre-scales, and for pitch-down settles/targets.
        for _, plan in jobs:
            if plan[0] == "resample":
                return True
            if plan[0] == "pitch":
                _, prefilter, pitched_rate, pitch_st, target, _ = plan
                cur_rate = (round(sample_rate * speed)
                            if prefilter is not None else sample_rate)
                if pitch_st >= 0:
                    prescale_to = (target if target is not None
                                   else pitched_rate)
                    if prescale_to != cur_rate:
                        return True
                elif target is not None or pitched_rate != cur_rate:
                    return True
        return False

    if not args.dry_run and pitch_plans and shutil.which("rubberband") is None:
        print(
            "Error: 'rubberband' not found. Pitch shifting (-p, -S, -D pitch)\n"
            "requires rubberband installed (apt/pacman/brew install rubberband).",
            file=sys.stderr,
        )
        return 127

    # SoX runs for every resample plan, for pitch-up pre-scales, and for
    # pitch-down settles/targets; rubberband only ever shifts pitch.
    if (not args.dry_run and _needs_sox()
            and shutil.which("sox") is None):
        print(
            "Error: 'sox' not found. Resampling (-R) and pitch rate steps\n"
            "require SoX installed (apt/pacman/brew install sox).",
            file=sys.stderr,
        )
        return 127

    # Output filenames carry the shift ([Nightcore N ...]) and, when
    # resampling now, rate ([96kHz]) appended to a base; quality lives
    # in the pipeline and explicit template {quality} only, never in
    # names. --autorename builds the
    # base from tags ("Artist - Title", artist prefix omitted when the
    # source has no artist tag) or from a user TEMPLATE with bare
    # {artist}/{title}/{diff}/{quality}/{resample}/{denightcore} slots;
    # by default the base is the input's own stem. The Title tag
    # follows the tag template (default "Title [Nightcore diff]");
    # the Artist tag always rides through untouched (source-mapped).
    # Names are sanitized for Linux/Windows-safe filenames. Outputs land
    # in the current directory. -D rebuilds names from tags (Artist - Title
    # [denightcored BP/NBP], plus [rate] when resampling now); the
    # filename is never trusted.
    if args.denightcore:
        # Restored title: the title tag still carries the old shift and
        # display groups (and possibly stale quality/rate groups) from
        # when the file was nightcored; strip them all. The fresh
        # [denightcored BP/NBP] verdict is added by compute_new_title.
        original_title = strip_denightcore_tags(strip_quality_rate_tags(
            split_shift_tags(original_title)[0]))
        _auto = getattr(args, "autorename", None)
        if _auto is not None and _auto != "":
            name_base = render_autorename(
                args, artist, original_title, input_path.stem,
                diff=diff_bare, quality=quality_bare,
                resample=resample_bare, denightcore=denightcore_bare)
            # Custom TEMPLATE already carries its own slots.
            jobs = [("", plan) for _, plan in jobs]
        else:
            base = (f"{artist} - {original_title}" if artist
                    else original_title)
            if not base.strip():
                base = strip_denightcore_tags(input_path.stem)
            rate = f" [{resample_bare}]" if resample_bare else ""
            jobs = [(f"{rate} [{denightcore_bare}]", plan)
                    for _, plan in jobs]
            name_base = base
    elif getattr(args, "autorename", None) is None:
        name_base = clean_source_title(input_path.stem)
    elif args.autorename == "":
        # Bare --autorename: legacy "Artist - Title [tags]" (script groups
        # leave the base only where the suffix re-adds them -- see
        # autorename_base); otherwise inherited tags stay put.
        _base_title = autorename_base(
            artist, original_title, strip_denightcore_tags(input_path.stem),
            drop_shift=bool(diff_bare), drop_rate=bool(resample_bare))
        name_base = _base_title
    else:
        name_base = render_autorename(
            args, artist, original_title, input_path.stem,
            diff=diff_bare, quality=quality_bare, resample=resample_bare,
            denightcore=denightcore_bare)
        # Custom TEMPLATE already carries its own slots: suffixes below
        # must not be appended again.
        if out_dir is not None:
            parent = out_dir
        else:
            parent = single_parent(args, input_path)
        outputs = [(suffix, parent / (sanitize_filename(name_base)
                                      + out_ext)) for suffix, _ in jobs]
        if args.output is not None and out_dir is None:
            outputs = [(suffix, parent / args.output)
                       for suffix, _ in outputs]
        for suffix, output_path in outputs:
            if output_path == input_path:
                print("Error: output would overwrite input, aborting",
                      file=sys.stderr)
                return 1
        # Skip past the legacy suffix-append path below.
        name_base = None
    if name_base is not None:
        if out_dir is not None:
            parent = out_dir
        else:
            parent = single_parent(args, input_path)
        outputs = []
        for suffix, _ in jobs:
            outputs.append((
                suffix,
                parent / (sanitize_filename(name_base + suffix) + out_ext),
            ))

    if out_dir is not None or ctx.get("batch_total", 0) > 1:
        # Batch mode funnels many inputs into one directory, so
        # tag-derived names can collide (duplicate files with identical
        # tags). The first file keeps the name; later ones get
        # " (2)", " (3)", ... Same for several explicit inputs sharing
        # the current directory (or one --output-folder).
        outputs = [(suffix, claim_output_path(ctx, output_path))
                   for suffix, output_path in outputs]

    if args.output is not None and out_dir is None:
        # -o overrides the computed name verbatim (batch+-o is rejected
        # in main, so exactly one job exists here).
        outputs = [(suffix, parent / args.output)
                   for suffix, _ in outputs]

    for suffix, output_path in outputs:
        if output_path == input_path:
            print("Error: output would overwrite input, aborting", file=sys.stderr)
            return 1

    if args.dry_run:
        be = backend.get("kind") if backend else None
        for suffix, output_path in outputs:
            title = compute_new_title(
                tmpl, title_base, original_title, suffix,
                diff=diff_bare, denightcore=denightcore_bare,
                title_verbatim=title_verbatim)
            flag_c = (cover_flag if (cover_flag is not None
                                     and out_ext in COVER_FORMATS)
                      else None)
            probe_c = None
            if out_ext in COVER_FORMATS:
                cc = cover_codec_name(input_path)
                probe_c = cc if cc in COVER_CODECS else None
            mode, cover = describe_output_tags(
                tmpl, args.no_delete_tags, len(ff_extra), be, out_ext,
                flag_c, probe_c)
            preview_output(output_path, title, mode, cover,
                           head=batch_prefix(ctx))
        return 0

    for suffix, output_path in outputs:
        if output_path.exists() and not args.overwrite:
            if out_dir is not None and all(
                    p.exists() for _, p in outputs):
                # Batch rerun without -y: skip what's already encoded
                # (add files freely; -y re-encodes everything).
                print(f"Skipping {input_path.name} (output exists, "
                      "use -y to re-encode)", file=sys.stderr)
                ctx["skipped"] = ctx.get("skipped", 0) + 1
                return 0
            print(
                f"Error: output already exists: {output_path} (use -y to overwrite)",
                file=sys.stderr,
            )
            return 1

    try:
        # qaac (CoreAudio, often run via wine) encodes ALAC when present;
        # otherwise ffmpeg's alac encoder is used. qaac needs integer PCM.
        # AAC also prefers qaac (announced once in main); its fallback
        # warning already fired there.
        be_kind = backend.get("kind") if backend else None
        qaac_version = find_qaac() if args.format == "alac" else None
        use_qaac = (qaac_version is not None
                    or (args.format == "aac" and be_kind == "qaac"))
        if args.format == "alac":
            if use_qaac:
                say(f"Encoding ALAC with {qaac_version}")
            else:
                redist = qaac_redist_hint()
                detail = (f"qaac is installed but unusable ({redist})"
                          if redist else
                          "qaac not found")
                say(f"Note: {detail}, encoding ALAC with ffmpeg")
        no_delete = args.no_delete_tags
        # Template strip mode drops every unlisted tag; --no-delete-tags
        # maps source tags first so the template only overrides.
        strip = tmpl is not None and not no_delete
        if out_ext == ".opus" and tmpl is not None:
            # Same canonical fold as opus_tags_for below: the ffmpeg opus
            # muxer writes keys verbatim, so the movenc alias must not
            # reach it either.
            ff_extra = fold_album_artist(ff_extra)
        extra_tags = ff_extra if tmpl is not None else dict(ff_extra)
        if be_kind == "opusenc" and (tmpl is None or no_delete):
            # opusenc tags natively (ffmpeg can't write opus pictures),
            # so it needs the full source tag set up front: the rebuilt
            # comments below must not lose anything --discard-comments
            # cleared from the temp wav.
            src_tags, _ = read_all_tags(input_path)
            merged = {str(k).lower(): v for k, v in src_tags.items()}
            if tmpl is not None:
                merged.update({str(k).lower(): v
                               for k, v in ff_extra.items()})
            extra_tags = merged
        # Final-encode triple for ffmpeg-backed outputs: (codec, extra
        # ffmpeg args, forced depth). Lossy+ffmpeg passes only the user's
        # --lossy-opts and skips depth forcing (encoders manage it);
        # lossless passes the format compression defaults (FLAC 12).
        if is_lossy_output and be_kind == "ffmpeg":
            enc_codec = out_codec
            enc_args = list(lossy_opts)
            enc_fmt = None
        else:
            enc_codec = out_codec
            enc_args = list(OUTPUT_FORMATS[args.format].get("ffmpeg_args")
                            or [])
            enc_fmt = depth_fmt

        meta_src = None if strip else input_path

        def finalize_ffmpeg(wav_path, final_path, title, codec, sample_fmt,
                            ffmpeg_args=None, drop_video=False,
                            drop_tags=None, dither=None, bitexact=True):
            run_ffmpeg(wav_path, final_path, None, codec, title,
                       overwrite=args.overwrite,
                       metadata_from=meta_src,
                       sample_fmt=sample_fmt,
                       with_cover=map_cover and meta_src is not None,
                       cover_file=(flag_cover or
                                   (cover_tmp if strip else None)),
                       strip_tags=strip,
                       extra_tags=extra_tags or None,
                       ffmpeg_args=ffmpeg_args,
                       drop_video=drop_video,
                       drop_tags=drop_tags,
                       dither=dither,
                       bitexact=bitexact)
            normalize_cover(final_path)

        def normalize_cover(final_path):
            if out_ext == ".flac" and (
                    norm_image is not None or want_c is not None
                    or want_d is not None or stub_encoder is not None):
                fixed = []
                if norm_image is not None:
                    fixed.append("Cover (front) picture")
                if want_c is not None or want_d is not None:
                    fixed.append("COMMENT/DESCRIPTION")
                if stub_encoder is not None:
                    fixed.append("ENCODER")
                if not flac_postprocess(final_path, norm_image, norm_mime,
                                        want_c, want_d, stub_encoder):
                    print(f"Warning: metaflac fixes failed on {final_path}",
                          file=sys.stderr)
                elif fixed:
                    say(f"  metaflac: {', '.join(fixed)}")

        def finalize_qaac(wav_path, final_path, title, alac=True,
                          extra_args=(), dither=None):
            if sample_is_float(wav_path):
                int_wav = temp_wav()
                run_ffmpeg(wav_path, int_wav, None, depth_pcm,
                           title, overwrite=True, dither=dither)
                wav_path = int_wav
            qtmp = temp_file("m4a")
            say(f"  qaac encode {'alac' if alac else 'aac'}")
            run_qaac_alac(wav_path, qtmp, alac=alac, extra_args=extra_args)
            # Tags and cover art go through ffmpeg, never qaac. AAC is
            # lossy: drop stale source encoder tags (ALAC keeps them).
            # Lossy keeps the encoder's own tags unless templated.
            finalize_ffmpeg(qtmp, final_path, title, "copy", None,
                            drop_tags=(*scrub_tags, *(STALE_TAGS if not alac else ())) or None,
                            bitexact=tmpl is not None or alac)

        def finalize_lame(wav_path, final_path, title):
            ltmp = temp_file("mp3")
            say("  lame encode mp3")
            run_lame(wav_path, ltmp, lossy_opts)
            # Tags and cover art go through ffmpeg, never lame.
            finalize_ffmpeg(ltmp, final_path, title, "copy", None,
                            drop_tags=(*scrub_tags, *STALE_TAGS) or None,
                            bitexact=tmpl is not None)

        def opus_tags_for(title):
            if tmpl is not None and not no_delete:
                tags = fold_album_artist({
                    str(k).lower(): v for k, v in ff_extra.items()
                    if str(k).lower() != "encoder"})
            else:
                # Full source set, lowercased (opus comments are
                # case-insensitive): title/artist are overridden below,
                # stale source encoder tags describe the input, not this
                # encode. Scrubbed marks (--no-stamp/--no-pipeline) are
                # dropped from mapped source tags too (template-explicit
                # values above still apply).
                tags = {str(k).lower(): v for k, v in extra_tags.items()}
                tags.pop("encoder", None)
                tags.pop("encoding_settings", None)
                if STAMP_TAG in scrub_tags:
                    tags.pop("description", None)
                if PIPELINE_TAG in scrub_tags:
                    tags.pop("encoder_settings", None)
                tags = fold_album_artist(tags)
                if artist:
                    tags["artist"] = artist
            tags["title"] = title
            return tags

        def finalize_opusenc(wav_path, final_path, title):
            cover = flag_cover or cover_tmp
            say("  opusenc encode")
            run_opusenc(wav_path, final_path, opus_tags_for(title),
                        cover, lossy_opts)

        def finalize_wav(wav_path, final_path, title, codec, sample_fmt):
            drop = (is_lossy_output and be_kind == "ffmpeg"
                    and args.format == "opus")
            # Triangular dither on processed 16-bit encodes (resample/pitch
            # temps always hold fractional values, so truncation would
            # correlate; deeper targets keep sub-LSB detail unaided).
            # Float-accepting lossy encoders get untouched float, direct
            # paths stay bit-perfect.
            dither = ("s16" if (not is_lossy_output
                                and sample_fmt == "s16") else None)
            finalize_ffmpeg(wav_path, final_path, title, codec,
                            sample_fmt, ffmpeg_args=enc_args or None,
                            drop_video=drop,
                            drop_tags=(*scrub_tags, *(STALE_TAGS if is_lossy_output else ())) or None,
                            dither=dither,
                            bitexact=tmpl is not None or not is_lossy_output)

        # Cover art: --cover wins outright; otherwise the original picture
        # is reused (extracted file for qaac/lame/opusenc, mapped stream
        # for ffmpeg). Opus pictures go via opusenc --picture (ffmpeg
        # ogg-opus rejects video).
        cover_ok_format = out_ext in (".flac", ".m4a", ".mp3", ".opus")
        flag_cover = cover_flag if (cover_flag is not None
                                    and cover_ok_format) else None
        if cover_flag is not None and not cover_ok_format:
            print(f"Warning: --cover ignored for {out_ext} output "
                  "(format cannot carry pictures)", file=sys.stderr)
        if (flag_cover is not None and out_ext == ".opus"
                and be_kind == "ffmpeg"):
            # ffmpeg's ogg-opus muxer rejects video streams outright;
            # only opusenc can attach pictures to .opus.
            print("Warning: --cover ignored for ffmpeg Opus output "
                  "(use opusenc backend for Opus pictures)", file=sys.stderr)
            flag_cover = None
        if stub_encoder is not None and out_ext != ".flac":
            print("Warning: custom 'encoder' tags only supported for FLAC "
                  "output; skipped", file=sys.stderr)
        orig_cover = None
        if flag_cover is None and cover_ok_format:
            cc = cover_codec_name(input_path)
            if cc in COVER_CODECS:
                orig_cover = cc
        # ffmpeg ogg-opus takes no pictures at all; opusenc handles them.
        map_cover = (flag_cover is None and orig_cover is not None
                     and cover_ok_format and out_ext != ".opus")
        # FLAC finals get post-processing via metaflac: Cover (front)
        # type (ffmpeg never propagates picture types), exact
        # COMMENT/DESCRIPTION (ffmpeg merges COMMENT into DESCRIPTION on
        # write), and custom ENCODER values (ffmpeg stamps its own or,
        # under bitexact, nothing). m4a covr atoms carry no type, so
        # only flac qualifies.
        want_c, want_d = flac_comment_wants(tmpl, ff_extra, input_path,
                                              out_ext, no_delete)
        if stamp is not None and out_ext == ".flac":
            # metaflac writes DESCRIPTION exactly, immune to ffmpeg's
            # COMMENT-into-DESCRIPTION merge on FLAC writes.
            want_d = stamp
        if stamp is None and ctx.get("no_stamp") and out_ext == ".flac":
            # Scrubbed provenance must not be restored from the source by
            # the metaflac pass (unless the template sets it explicitly).
            if not (tmpl is not None and "description" in tmpl):
                want_d = None
        need_meta = (out_ext == ".flac"
                     and (flag_cover is not None or orig_cover is not None
                          or want_c is not None or want_d is not None
                          or stub_encoder is not None))
        if need_meta and missing_metaflac():
            return 127
        norm_image, norm_mime = None, None
        if need_meta and flag_cover is not None:
            norm_image = flag_cover
            norm_mime = _IMAGE_MIME[flag_cover.suffix.lower()]

        tempdir = None
        cli_backend = be_kind in ("lame", "opusenc", "qaac")
        if (use_qaac or cli_backend
                or any(plan[0] != "direct" for _, plan in jobs)
                or (need_meta and flag_cover is None
                    and orig_cover is not None)):
            tempdir = Path(tempfile.mkdtemp(prefix="nightcore-"))
        # One-line backend label for the per-job stage headers below.
        if use_qaac:
            enc_label = "qaac"
        elif be_kind is not None:
            enc_label = be_kind
        else:
            enc_label = out_codec
        # 32-bit int sources need s32 temps (f32's 24-bit mantissa would
        # eat LSBs); everything else rides exact in f32.
        tmp_codec = ("pcm_s32le" if (bits is not None and bits > 24)
                     else "pcm_f32le")
        # Triangular-dither target for processed int encodes through
        # qaac's float-int conversion temp: 16-bit only (deeper targets
        # keep sub-LSB detail unaided). Direct paths pass None to stay
        # bit-perfect.
        qaac_dither = ("s16" if depth_pcm == "pcm_s16le" else None)

        # Pipeline record (--no-pipeline disables): tool versions and the
        # stage arguments they ran join the params stashed earlier, in
        # pipeline order. Assembled here (not earlier) so backend choices
        # above are known; extra_tags below picks it up before encoding.
        if not ctx.get("no_pipeline"):
            _tools = ["ffmpeg"]
            if _needs_sox():
                _tools.append("sox")
            if pitch_plans:
                _tools.append("rubberband")
            if use_qaac:
                _tools.append("qaac")
            elif be_kind in ("lame", "opusenc"):
                _tools.append(be_kind)
            if need_meta:
                _tools.append("metaflac")
            _tool_versions = [(t, tool_version(t)) for t in _tools]
            if use_qaac:
                _tb = coreaudio_toolbox_version()
                if _tb is not None:
                    _tool_versions.append(("CoreAudioToolbox", _tb))
            ff_extra[PIPELINE_TAG] = build_pipeline(
                **pipeline_params,
                tools=_tool_versions,
                stages=stage_records(
                    jobs, sample_rate=sample_rate, speed=speed,
                    use_qaac=use_qaac, qaac_dither=qaac_dither,
                    cli_backend=cli_backend,
                    is_lossy_output=is_lossy_output, enc_fmt=enc_fmt))
            # extra_tags was snapshotted (or merged for opusenc) before the
            # pipeline record existed; template runs ride along in ff_extra
            # itself, same object, so top them up otherwise (ours win over
            # same-named source tags, matching the ffmpeg -metadata order).
            if tmpl is None:
                for _k in (STAMP_TAG, PIPELINE_TAG):
                    if _k in ff_extra:
                        extra_tags = {**extra_tags, _k: ff_extra[_k]}

        def ffmpeg_step(audio_filter):
            # Announce an ffmpeg decode/speedup step (its log is piped
            # away under -q, so without this the stage goes unseen).
            if audio_filter is not None and "asetrate" in audio_filter:
                say(f"  ffmpeg {audio_filter}")
            else:
                say("  ffmpeg decode")

        try:
            step = 0

            def temp_file(ext):
                nonlocal step
                step += 1
                assert tempdir is not None
                return tempdir / f"step{step}.{ext}"

            def temp_wav():
                return temp_file("wav")

            # Attached pictures don't survive temp WAVs: extract the original
            # once for template-mode cover replacement; the same file feeds
            # Cover (front) normalization below. Plain ffmpeg finals map
            # covers straight from the source.
            cover_tmp = None
            need_cover_file = (need_meta or cli_backend
                               or (tmpl is not None and cover_ok_format
                                   and any(plan[0] != "direct"
                                           for _, plan in jobs)))
            if (flag_cover is None and orig_cover is not None
                    and need_cover_file):
                cover_tmp = extract_cover_tmp(
                    input_path, orig_cover, tempdir)
                if (cover_tmp is not None and norm_image is None
                        and out_ext == ".flac"):
                    norm_image = cover_tmp
                    norm_mime = ("image/jpeg"
                                 if cover_tmp.suffix == ".jpg"
                                 else "image/png")

            for (suffix, plan), (_, output_path) in zip(jobs, outputs):
                new_title = compute_new_title(
                    tmpl, title_base, original_title, suffix,
                    diff=diff_bare, denightcore=denightcore_bare,
                    title_verbatim=title_verbatim)
                kind = plan[0]
                if kind == "direct":
                    desc = ("speedup" if plan[1] is not None else "convert")
                elif kind == "resample":
                    desc = f"resample to {format_rate(plan[2])}"
                else:
                    desc = f"pitch {plan[3]:+}st"
                    if plan[4] is not None:
                        desc += f" + resample to {format_rate(plan[4])}"
                head = batch_prefix(ctx)
                say(f"{head}{output_path.name}: {desc} "
                    f"-> {args.format} via {enc_label}")
                _active_outputs.add(output_path)
                if kind == "direct":
                    if use_qaac:
                        tmp = temp_wav()
                        ffmpeg_step(plan[1])
                        run_ffmpeg(input_path, tmp, plan[1], depth_pcm,
                                   new_title, overwrite=True)
                        is_alac = (args.format == "alac")
                        finalize_qaac(tmp, output_path, new_title,
                                      alac=is_alac,
                                      extra_args=([] if is_alac
                                                  else lossy_opts))
                    elif be_kind == "lame":
                        tmp = temp_wav()
                        ffmpeg_step(plan[1])
                        run_ffmpeg(input_path, tmp, plan[1], tmp_codec,
                                   new_title, overwrite=True)
                        finalize_lame(tmp, output_path, new_title)
                    elif be_kind == "opusenc":
                        tmp = temp_wav()
                        ffmpeg_step(plan[1])
                        run_ffmpeg(input_path, tmp, plan[1], tmp_codec,
                                   new_title, overwrite=True)
                        finalize_opusenc(tmp, output_path, new_title)
                    elif is_lossy_output and be_kind == "ffmpeg":
                        ffmpeg_step(plan[1])
                        run_ffmpeg(input_path, output_path, plan[1],
                                   enc_codec, new_title,
                                   overwrite=args.overwrite,
                                   sample_fmt=enc_fmt,
                                   cover_file=flag_cover,
                                   strip_tags=strip,
                                   extra_tags=extra_tags or None,
                                   ffmpeg_args=enc_args,
                                   drop_video=(args.format == "opus"),
                                   drop_tags=(*scrub_tags, *STALE_TAGS) or None,
                                   bitexact=tmpl is not None)
                        normalize_cover(output_path)
                    else:
                        ffmpeg_step(plan[1])
                        run_ffmpeg(input_path, output_path, plan[1], out_codec,
                                   new_title, overwrite=args.overwrite,
                                   sample_fmt=depth_fmt,
                                   cover_file=flag_cover,
                                   strip_tags=strip,
                                   extra_tags=extra_tags or None,
                                   drop_tags=(*scrub_tags,) or None,
                                   ffmpeg_args=enc_args or None)
                        normalize_cover(output_path)
                elif kind == "resample":
                    _, prefilter, target = plan
                    tmp1, tmp2 = temp_wav(), temp_wav()
                    ffmpeg_step(prefilter)
                    run_ffmpeg(input_path, tmp1, prefilter, tmp_codec,
                               new_title, overwrite=True)
                    say(f"  SoX resample to {format_rate(target)}")
                    run_sox_rate(tmp1, tmp2, target)
                    if use_qaac:
                        is_alac = (args.format == "alac")
                        finalize_qaac(tmp2, output_path, new_title,
                                      alac=is_alac,
                                      extra_args=([] if is_alac
                                                  else lossy_opts),
                                      dither=qaac_dither)
                    elif be_kind == "lame":
                        finalize_lame(tmp2, output_path, new_title)
                    elif be_kind == "opusenc":
                        finalize_opusenc(tmp2, output_path, new_title)
                    else:
                        finalize_wav(tmp2, output_path, new_title,
                                     enc_codec, enc_fmt)
                elif kind == "pitch":
                    _, prefilter, pitched_rate, pitch_st, target, rb_job = plan
                    tmp1 = temp_wav()
                    ffmpeg_step(prefilter)
                    run_ffmpeg(input_path, tmp1, prefilter, tmp_codec,
                               new_title, overwrite=True)
                    # tmp1 rate: post-prefilter if any, else the input rate.
                    cur_rate = (round(sample_rate * speed)
                                if prefilter is not None else sample_rate)
                    cur = tmp1
                    if pitch_st >= 0:
                        # Shift up: SoX pre-scales first, otherwise shifted
                        # highs alias straight back into the audible band.
                        # With -R the target already sits above the pitched
                        # rate, so one pass covers headroom and settling.
                        prescale_to = (target if target is not None
                                       else pitched_rate)
                        if prescale_to != cur_rate:
                            tmp2 = temp_wav()
                            if target is not None:
                                say(f"  SoX prescale to {format_rate(target)} "
                                    "(pitch headroom + resample)")
                            else:
                                say(f"  SoX prescale to {format_rate(pitched_rate)} "
                                    "for pitch headroom")
                            run_sox_rate(cur, tmp2, prescale_to)
                            cur = tmp2
                            cur_rate = prescale_to
                    tmp3 = temp_wav()
                    say(f"  rubberband pitch {pitch_st:+}st")
                    run_rubberband(cur, tmp3, pitch_st, rb_job)
                    cur = tmp3
                    cur, _ = tame_hot_wav(cur, temp_wav)
                    # Settle the rate afterwards: the explicit target, else
                    # the pitched rate for down-shifts (up-shifts already
                    # sit at it from the pre-scale above).
                    settle = (target if target is not None
                              else (pitched_rate if pitch_st < 0 else None))
                    if settle is not None and settle != cur_rate:
                        tmp4 = temp_wav()
                        say(f"  SoX settle to {format_rate(settle)}")
                        run_sox_rate(cur, tmp4, settle)
                        cur = tmp4
                    if use_qaac:
                        is_alac = (args.format == "alac")
                        finalize_qaac(cur, output_path, new_title,
                                      alac=is_alac,
                                      extra_args=([] if is_alac
                                                  else lossy_opts),
                                      dither=qaac_dither)
                    elif be_kind == "lame":
                        finalize_lame(cur, output_path, new_title)
                    elif be_kind == "opusenc":
                        finalize_opusenc(cur, output_path, new_title)
                    else:
                        finalize_wav(cur, output_path, new_title,
                                     enc_codec, enc_fmt)
                ctx["converted"] = ctx.get("converted", 0) + 1
                _active_outputs.discard(output_path)
        finally:
            if tempdir is not None:
                shutil.rmtree(tempdir, ignore_errors=True)
        if args.denightcore:
            # Bit-perfect verdict for the undo: a pure sample-rate
            # speedup inverts bit-exactly (lossless, no resampling
            # anywhere); anything else degrades, with stacked reasons.
            # Must match denightcore_verdict() in plan.py, which names
            # the output files.
            bad = []
            if d_st != 0.0:
                bad.append("pitch inversion is lossy "
                           "(rubberband resynthesis)")
            if target_rate is not None:
                bad.append(f"resampled to {format_rate(target_rate)} "
                           "(original rate unrecoverable)")
            _origin = pipeline_origin(denight_raw)
            if pipeline_has(denight_raw, "resample"):
                bad.append("forward job was resampled "
                           "(original rate unrecoverable)")
            if is_lossy_output:
                bad.append(f"re-encoded to {args.format} (lossy)")
            if codec in LOSSY_CODECS:
                bad.append(f"source is lossy ({codec})")
            if (_origin["codec"] is not None
                    and _origin["codec"].lower() in LOSSY_CODECS):
                bad.append(f"forward source was lossy ({_origin['codec']})")
            if bad:
                print(f"Warning: denightcore of {input_path.name} is NOT "
                      f"bit-perfect: {'; '.join(bad)}", file=sys.stderr)
            else:
                say(f"Bit-perfect denightcore of {input_path.name}")
        return 0

    except subprocess.CalledProcessError as e:
        print(f"\nffmpeg/sox/rubberband/qaac/lame/opusenc failed with exit code {e.returncode}", file=sys.stderr)
        return 1

    except FileNotFoundError:
        print("Error: a required tool went missing mid-run. ffmpeg is always needed (plus sox for resampling, rubberband for pitch).", file=sys.stderr)
        return 127
