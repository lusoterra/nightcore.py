"""The stream-copy retag workflow."""

from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tempfile

from .audio import cover_codec_name, extract_cover_tmp, format_rate, get_audio_info, get_sample_rate
from .backends import flac_postprocess, missing_metaflac, run_ffmpeg, tool_version
from .constants import COVER_CODECS, COVER_FORMATS, LOSSY_CODECS, PIPELINE_TAG, STAMP_TAG, _IMAGE_MIME
from .naming import claim_output_path, recover_filename_quality, single_parent, split_shift_tags, strip_empty_display_tags, strip_quality_rate_tags
from .tags import apply_template_tags, augment_pipeline_tools, autorename_base, backfill_stamp_settings, compute_new_title, describe_output_tags, display_suffix, ensure_pipeline_magic, flac_comment_wants, fold_album_artist, format_diff_label, get_artist, get_title, pipeline_origin, read_format_tag, read_shift_source, render_autorename, sanitize_filename, tool_stamp
from .util import _active_outputs, batch_prefix, preview_output, say

def run_renametag(args, input_path, out_dir, ctx):
    """Rewrite tags and rename via stream copy (no audio work).

    The output keeps the input's container (-F is ignored). Batch copies
    land flat in ./renametag/ (originals stay); single-file runs copy
    there too, unless --in-place moves alongside the input (asks [y/N]
    first) or -o names the copy verbatim (originals always stay).
    Names come from --autorename (bare: Artist - Title; TEMPLATE: custom
    {artist}/{title} render) or the input stem, with no conversion params
    appended. Title/Artist follow
    --tag-template/--no-delete-tags (suffix is
    empty: nothing was processed). Pictures are preserved by
    mapping, replaced by --cover; opus pictures are impossible through
    ffmpeg, so those fail loudly. Same-name rewrites go through a temp
    file + atomic replace. Same-stem .lrc lyrics follow the master:
    renamed (single/in-place) or copied (batch) next to the audio
    output, before the audio itself moves. Returns a process-style
    exit code.
    """
    tmpl = ctx["tmpl"]
    cover_flag = ctx["cover_flag"]
    no_delete = args.no_delete_tags

    input_path = Path(input_path)
    if not input_path.is_file():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        return 1
    if input_path.suffix.lower() == ".lrc":
        print("Error: -N/--renametag needs audio "
              "(no tags to rewrite in .lrc)", file=sys.stderr)
        return 1
    out_ext = input_path.suffix.lower()

    stem = input_path.stem
    original_title = get_title(input_path, fallback=stem)
    artist = get_artist(input_path)
    title_base = original_title
    ff_extra = {}
    stub_encoder = None
    title_verbatim = False
    # Read the existing stamp up front: it feeds both preservation below
    # and the {diff} slot. The slot reflects the recovered shift for this
    # file -- exact Settings parse first, filename tags as fallback, else
    # empty (a plain retag then renders no shift, same as before).
    # Existing tags up front: the pipeline feeds preservation below and
    # the {diff} slot reflects the recovered shift for this file --
    # exact pipeline parse first, legacy DESCRIPTION Settings, filename
    # tags as fallback, else empty (a plain retag then renders no shift,
    # same as before).
    existing_pipe = read_format_tag(input_path, PIPELINE_TAG)
    existing_desc = read_format_tag(input_path, STAMP_TAG)
    _parsed, _ = read_shift_source(input_path)
    if _parsed is not None:
        _r_speed, _r_st = _parsed
    else:
        _, _r_speed, _r_st, _r_found, _ = split_shift_tags(stem)
        if not _r_found:
            _r_speed, _r_st = 1.0, 0.0
    recovered_diff = format_diff_label(_r_speed, _r_st)
    # Filename quality groups: the visible origin record (a FLAC wrapper
    # probes as flac). Used for the pipeline origin check below and the
    # canonical -N filename suffix.
    _forge = recover_filename_quality(stem)
    if tmpl is not None:
        (title_base, ff_extra, stub_encoder, artist,
         original_title, title_verbatim) = apply_template_tags(
             tmpl, input_path, original_title, artist,
             keep_missing=args.no_delete_tags,
             slots={"diff": recovered_diff, "quality": "",
                    "resample": "", "denightcore": ""})
        if out_ext != ".m4a":
            # Canonical albumartist everywhere except movenc, which drops
            # the bare spelling (hence the alias above).
            ff_extra = fold_album_artist(ff_extra)
    if not ctx.get("no_stamp"):
        # Provenance: preserved as-is when present (unless the template
        # sets it explicitly), else fresh branding. A template
        # description is overridden, same as conversions.
        if existing_desc and STAMP_TAG not in ff_extra:
            ff_extra[STAMP_TAG] = existing_desc
        elif STAMP_TAG not in ff_extra:
            ff_extra[STAMP_TAG] = tool_stamp()
    if not ctx.get("no_pipeline"):
        # Pipeline: preserved as-is when present (retags change no audio,
        # so the transforming run owns the record) -- unless the filename
        # proves a different lossy origin, in which case the record is
        # rebuilt from the filename (a wrong origin would mislead -D).
        # Old records gain the magic marker (and missing stage tools)
        # here so strict readers accept them afterwards; content is
        # otherwise untouched. A legacy DESCRIPTION Settings line
        # migrates into the split form, keeping its original provenance
        # line. Otherwise the shift is recovered from the filename tags
        # when present -- old files gain a real pipeline and become
        # -D-able -- else no pipeline tag is written at all.
        _pcodec = pipeline_origin(existing_pipe or "")["codec"]
        if (_forge["codec"] is not None and (
                _pcodec is None or _pcodec.lower().replace(
                    "-", "_") != _forge["codec"])):
            _info = get_audio_info(input_path)
            reco = backfill_stamp_settings(
                stem, sample_rate=get_sample_rate(input_path),
                bits=_info[2], codec=_info[0],
                out_format=out_ext.lstrip("."),
                origin=_forge)
            if reco is not None:
                ff_extra[PIPELINE_TAG] = reco
        elif existing_pipe and existing_pipe.strip():
            ff_extra[PIPELINE_TAG] = augment_pipeline_tools(
                ensure_pipeline_magic(existing_pipe))
        else:
            _, sep, rest = (existing_desc or "").partition("\n")
            if sep and rest.strip().startswith("Settings:"):
                ff_extra[PIPELINE_TAG] = augment_pipeline_tools(
                    ensure_pipeline_magic(
                        rest.strip().removeprefix("Settings:").strip()))
                if (existing_desc and not ctx.get("no_stamp")
                        and STAMP_TAG not in ff_extra):
                    ff_extra[STAMP_TAG] = existing_desc.split("\n")[0]
            else:
                _info = get_audio_info(input_path)
                reco = backfill_stamp_settings(
                    stem, sample_rate=get_sample_rate(input_path),
                    bits=_info[2], codec=_info[0],
                    out_format=out_ext.lstrip("."),
                    origin=_forge)
                if reco is not None:
                    ff_extra[PIPELINE_TAG] = reco
    stamp = ff_extra.get(STAMP_TAG)
    codec = get_audio_info(input_path)[0]
    if codec in LOSSY_CODECS:
        # Same stale-quality-title rule as conversions.
        original_title = strip_quality_rate_tags(original_title)
    new_title = compute_new_title(tmpl, title_base, original_title, "",
                                  title_verbatim=title_verbatim)
    _auto = getattr(args, "autorename", None)
    if _auto is None:
        name_base = strip_empty_display_tags(stem)
    elif _auto == "":
        # Bare --autorename: "Artist - Title" rebuilt canonically. The
        # diff prints in display form with any recovered resample tag;
        # recovered quality groups stay out of names (pipeline keeps
        # them). Idempotent across re-runs; unrecoverable slots simply
        # stay empty, foreign brackets survive.
        _rsuffix = ""
        if _forge["resample_hz"] is not None:
            _rsuffix = f" [{format_rate(_forge['resample_hz'])}]"
        _suffix = display_suffix(recovered_diff) + _rsuffix
        _rendered = autorename_base(
            artist, original_title, strip_empty_display_tags(stem),
            drop_shift=bool(recovered_diff),
            drop_rate=_forge["resample_hz"] is not None)
        name_base = _rendered + _suffix
    else:
        name_base = render_autorename(args, artist, original_title, stem)
    parent = (out_dir if out_dir is not None
              else (input_path.parent
                    if getattr(args, "in_place", False)
                    and args.output is None
                    else single_parent(args, input_path)))
    output_path = (parent
                   / (sanitize_filename(name_base) + out_ext))
    if out_dir is not None or ctx.get("batch_total", 0) > 1:
        output_path = claim_output_path(ctx, output_path)
    if args.output is not None and out_dir is None:
        output_path = parent / args.output

    # Lyrics sidecar follows the master: same-stem .lrc is renamed
    # (single/in-place) or copied (batch) next to the audio output,
    # before the audio itself moves (a failed audio step then heals
    # on rerun: the sidecar is already where it belongs).
    lrc_src = None
    for ext in (".lrc", ".LRC"):
        cand = input_path.with_name(input_path.stem + ext)
        if cand.is_file():
            lrc_src = cand
            break
    lrc_dest = None
    if lrc_src is not None:
        dest = output_path.with_name(
            output_path.stem + lrc_src.suffix)
        lrc_dest = dest if dest != lrc_src else None

    cover_ok = out_ext in COVER_FORMATS
    flag_cover = cover_flag if (cover_flag is not None
                                and cover_ok) else None
    if cover_flag is not None and not cover_ok:
        print(f"Warning: --cover ignored for {out_ext} output "
              "(format cannot carry pictures)", file=sys.stderr)
    orig_cover = None
    if flag_cover is None and cover_ok:
        cc = cover_codec_name(input_path)
        if cc in COVER_CODECS:
            orig_cover = cc
    if out_ext == ".opus" and (flag_cover is not None
                               or orig_cover is not None):
        print("Error: cannot renametag opus pictures without re-encoding "
              "(ffmpeg writes no pictures to opus); encode a normal "
              "opus output instead", file=sys.stderr)
        return 1
    strip = tmpl is not None and not no_delete
    keep = not strip
    if stub_encoder is not None and out_ext != ".flac":
        print("Warning: custom 'encoder' tags only supported for FLAC "
              "output; skipped", file=sys.stderr)

    mode, cover = describe_output_tags(
        tmpl, no_delete, len(ff_extra), None, out_ext,
        flag_cover, orig_cover)
    if args.dry_run:
        preview_output(output_path, new_title, mode, cover,
                       note=(" (in place)"
                             if output_path == input_path else ""),
                       head=batch_prefix(ctx))
        if lrc_src is not None:
            if lrc_dest is not None:
                print(f"  lyrics: {lrc_src.name} -> {lrc_dest.name}")
            else:
                print(f"  lyrics: {lrc_src.name} (unchanged)")
        return 0
    if output_path != input_path and output_path.exists() \
            and not args.overwrite:
        print(
            f"Error: output already exists: {output_path} "
            "(use -y to overwrite)",
            file=sys.stderr,
        )
        return 1
    if lrc_src is not None and lrc_dest is not None:
        if lrc_dest.exists() and not args.overwrite:
            print(
                f"Error: lyrics output already exists: {lrc_dest} "
                "(use -y to overwrite)",
                file=sys.stderr,
            )
            return 1
        say(f"  lyrics: {lrc_src.name} -> {lrc_dest.name}")
        try:
            if out_dir is not None or args.output is not None:
                shutil.copy2(lrc_src, lrc_dest)
            else:
                os.replace(lrc_src, lrc_dest)
        except OSError as e:
            print(f"Error: cannot move lyrics: {e}", file=sys.stderr)
            return 1

    want_c, want_d = flac_comment_wants(tmpl, ff_extra, input_path,
                                        out_ext, no_delete)
    if stamp is not None and out_ext == ".flac":
        # metaflac writes DESCRIPTION exactly, immune to ffmpeg's
        # COMMENT-into-DESCRIPTION merge on FLAC writes.
        want_d = stamp
    if stamp is None and ctx.get("no_stamp") and out_ext == ".flac":
        # Scrubbed provenance must not be restored from the source
        # (unless the template sets it explicitly).
        if not (tmpl is not None and "description" in tmpl):
            want_d = None
    need_meta = (out_ext == ".flac"
                 and (flag_cover is not None or orig_cover is not None
                      or want_c is not None or want_d is not None
                      or stub_encoder is not None))
    if need_meta and missing_metaflac():
        return 127

    map_cover = (flag_cover is None and orig_cover is not None
                 and out_ext != ".opus")
    meta_src = input_path if keep else None
    # Temp dir alongside the input: same-name rewrites finish with
    # os.replace, which cannot cross filesystem boundaries (/tmp vs
    # the library is the classic EXDEV case), so /tmp is not an option.
    # Dot-prefixed to stay out of listings; always removed on exit
    # (leftovers mean SIGKILL/terminal-close: safe to delete).
    tempdir = Path(tempfile.mkdtemp(prefix=".nightcore-renametag-",
                                    dir=input_path.parent))
    try:
        cover_tmp = None
        if (flag_cover is None and orig_cover is not None
                and out_ext != ".opus" and (need_meta or strip)):
            cover_tmp = extract_cover_tmp(
                input_path, orig_cover, tempdir)
        norm_image, norm_mime = None, None
        if need_meta:
            if flag_cover is not None:
                norm_image = flag_cover
                norm_mime = _IMAGE_MIME[flag_cover.suffix.lower()]
            elif cover_tmp is not None:
                norm_image = cover_tmp
                norm_mime = ("image/jpeg"
                             if cover_tmp.suffix == ".jpg"
                             else "image/png")
        say(f"{batch_prefix(ctx)}Renametag {output_path.name} (stream copy)")
        same_path = (output_path == input_path)
        final_path = (tempdir / f"renametag{out_ext}"
                      if same_path else output_path)
        _active_outputs.add(final_path)
        if tmpl is not None:
            _extra = ff_extra or None
        else:
            # Non-template runs map source tags; our marks ride in
            # extra_tags explicitly (or not at all, when scrubbed).
            _extra = {}
            if stamp is not None:
                _extra[STAMP_TAG] = stamp
            if PIPELINE_TAG in ff_extra:
                _extra[PIPELINE_TAG] = ff_extra[PIPELINE_TAG]
            _extra = _extra or None
        _scrub = []
        if ctx.get("no_stamp"):
            _scrub.append(STAMP_TAG)
        if ctx.get("no_pipeline"):
            _scrub.append(PIPELINE_TAG)
        run_ffmpeg(input_path, final_path, None, "copy", new_title,
                   overwrite=True,
                   metadata_from=meta_src,
                   with_cover=map_cover and meta_src is not None,
                   cover_file=(flag_cover or
                               (cover_tmp if strip else None)),
                   strip_tags=strip,
                   extra_tags=_extra,
                   drop_tags=tuple(_scrub) or None,
                   bitexact=True)
        if need_meta and not flac_postprocess(
                final_path, norm_image, norm_mime,
                want_c, want_d, stub_encoder):
            print(f"Warning: metaflac fixes failed on {final_path}",
                  file=sys.stderr)
        if same_path:
            os.replace(final_path, output_path)
        elif out_dir is None and args.output is None:
            # In-place move (prompted in main): drop the original once
            # its replacement is fully written. Copies (-o, folders)
            # always keep the original.
            input_path.unlink()
        _active_outputs.discard(final_path)
        ctx["renametagged"] = ctx.get("renametagged", 0) + 1
        return 0
    except subprocess.CalledProcessError as e:
        print(f"\nffmpeg failed with exit code {e.returncode}",
              file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("Error: ffmpeg went missing mid-run.", file=sys.stderr)
        return 127
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)
    return 0
