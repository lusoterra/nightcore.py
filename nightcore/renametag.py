"""The stream-copy retag workflow."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

from .audio import cover_codec_name, extract_cover_tmp, get_audio_info
from .backends import flac_postprocess, missing_metaflac, run_ffmpeg
from .constants import COVER_CODECS, COVER_FORMATS, LOSSY_CODECS, STAMP_TAG, _IMAGE_MIME
from .naming import claim_output_path, name_base_for, single_parent, strip_quality_rate_tags
from .tags import apply_template_tags, compute_new_title, describe_output_tags, flac_comment_wants, get_artist, get_title, sanitize_filename
from .util import _active_outputs, batch_prefix, preview_output, say

def run_renametag(args, input_path, out_dir, ctx):
    """Rewrite tags and rename via stream copy (no audio work).

    The output keeps the input's container (-F is ignored) and lands in
    out_dir (batch: ./renametag/ copies, originals stay) or alongside
    the input (single-file: a rename moves, the original is removed).
    Names come from --autorename (Artist - Title) or the input stem,
    with no conversion params appended; -o overrides the name. Title/tags
    follow --tag-template/--no-delete-tags/--simple-title/--autotag
    (suffix is empty: nothing was processed). Pictures are preserved by
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
    if tmpl is not None:
        (title_base, ff_extra, stub_encoder, artist,
         original_title) = apply_template_tags(
             tmpl, input_path, original_title, artist,
             keep_missing=args.no_delete_tags)
    stamp = ctx.get("stamp")
    if stamp is not None:
        # --stamp always wins, even over a template description (same
        # rationale as conversions: explicit per-run provenance mark).
        ff_extra[STAMP_TAG] = stamp
    codec = get_audio_info(input_path)[0]
    if codec in LOSSY_CODECS:
        # Same stale-quality-title rule as conversions.
        original_title = strip_quality_rate_tags(original_title)
    new_title = compute_new_title(args, tmpl, title_base, original_title, "")
    name_base = name_base_for(args.autorename, artist,
                                original_title, stem)
    parent = (out_dir if out_dir is not None
              else single_parent(args, input_path))
    output_path = (parent
                   / (sanitize_filename(name_base) + out_ext))
    if out_dir is not None:
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
            if out_dir is not None:
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
    # the library is the classic EXDEV case).
    tempdir = Path(tempfile.mkdtemp(prefix="nightcore-renametag-",
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
        run_ffmpeg(input_path, final_path, None, "copy", new_title,
                   overwrite=True,
                   metadata_from=meta_src,
                   with_cover=map_cover and meta_src is not None,
                   cover_file=(flag_cover or
                               (cover_tmp if strip else None)),
                   strip_tags=strip,
                   extra_tags=(ff_extra or None)
                   if tmpl is not None else
                   ({STAMP_TAG: stamp} if stamp is not None else None),
                   bitexact=True)
        if need_meta and not flac_postprocess(
                final_path, norm_image, norm_mime,
                want_c, want_d, stub_encoder):
            print(f"Warning: metaflac fixes failed on {final_path}",
                  file=sys.stderr)
        if same_path:
            os.replace(final_path, output_path)
        elif out_dir is None:
            # Single-file rename moves: drop the original once its
            # replacement is fully written (batch retags are copies).
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


