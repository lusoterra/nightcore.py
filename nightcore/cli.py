"""Argument parsing, orchestration, and batch loops."""

from pathlib import Path
import argparse
import math
import shlex
import shutil
import signal
import sys

from . import __version__, util
from .audio import speed_from_semitones
from .backends import detect_lossy_dialect, run_check, select_lossy_backend
from .constants import AUDIO_EXTS, COVER_ART_EXTS, COVER_ART_NAMES, LOSS_DEFAULTS, OUTPUT_FORMATS, _IMAGE_MIME
from .convert import run_conversion
from .naming import batch_op_dir
from .renametag import run_renametag
from .tags import parse_tag_template
from .util import _help_formatter, _on_sigint, missing_tools, say

def parse_arguments():
    parser = argparse.ArgumentParser(
        prog="nightcore",
        description="Change audio speed/pitch using ffmpeg, sox and rubberband",
        formatter_class=_help_formatter
    )

    parser.add_argument(
        "input",
        type=str,
        nargs="*",
        default=[],
        help="Input audio file(s) (not needed for --pitch-diff/--speed-diff;\n"
             ".lrc lyrics allowed with -s: retimed copy; several files\n"
             "share the same settings)"
    )

    parser.add_argument(
        "-s", "--speed",
        type=float,
        default=1.0,
        help="Speed factor (1.2 = 20%% faster, 0.8 = slower)"
    )

    parser.add_argument(
        "-r", "--reciprocal",
        action="store_true",
        default=False,
        help="Use 1/speed instead (undo a speedup)"
    )

    parser.add_argument(
        "-y", "--overwrite",
        action="store_true",
        default=False,
        help="Overwrite existing output files"
    )

    parser.add_argument(
        "-q", "--quiet",
        action="count",
        default=0,
        help="Drop tool output (-q keeps stage/progress lines, -qq keeps\n"
             "only warnings and errors; a failed tool's captured output\n"
             "always prints)"
    )

    parser.add_argument(
        "-R", "--resample",
        action="store_true",
        default=False,
        help=(
            "Resample to next standard rate via SoX\n"
            "(batch outputs to ./std-samplerate/[format]/;\n"
            "default keeps the non-standard rate, bit-perfect)"
        )
    )

    parser.add_argument(
        "-F", "--format",
        default="flac",
        metavar="FMT",
        help="Output format: flac (default), alac, mp3, opus, aac;\n"
             "with --mkvid: mkv (default) or mp4 (the video container)"
    )

    parser.add_argument(
        "--lossy-opts",
        type=str,
        default=None,
        metavar="OPTS",
        help=(
            "Encoder quality flags (shlex-split, = form only). Defaults:\n"
            "V0 (mp3), 128k (opus), TVBR 91 (qaac); ffmpeg AAC has none\n"
            "(fails loudly: pass explicit opts). Your opts replace the\n"
            "defaults wholesale. Flag dialect picks the encoder:\n"
            "native-CLI style (-V,\n"
            "--bitrate, ...) uses lame/opusenc/qaac, -X:a style uses ffmpeg"
        )
    )

    parser.add_argument(
        "--batch",
        type=str,
        nargs="?",
        const=".",
        default=None,
        metavar="DIR",
        help="Convert every audio file in DIR (default: .) instead of\none input; outputs go to ./encodes/[format]/"
    )

    parser.add_argument(
        "--stats",
        type=str,
        nargs="?",
        const=".",
        default=None,
        metavar="DIR",
        help="Print statistics for every audio file in DIR (default: .)\n"
             "at depth 1 and exit: lyrics coverage, speed average and\n"
             "distribution, shift source, pitch, quality/lossless counts,\n"
             "containers, source codecs, bitrates, depth, sample rates\n"
             "(deeper packs: add --recurse)"
    )

    parser.add_argument(
        "--recurse",
        type=int,
        nargs="?",
        const=-1,
        default=None,
        metavar="N",
        help="With --stats: descend into subdirectories (for Artist/Album\n"
             "packs). Bare flag scans everything; --recurse N caps at N\n"
             "levels (the folder itself is level 1)"
    )

    parser.add_argument(
        "-p", "--pitch",
        action="store_true",
        default=False,
        help=(
            "Pitch-shift only, keep duration (needs rubberband;\n"
            "a modifier on -s/-S: the amount comes from there)\n"
            "Labelled [Nst] rounded to 2 decimals for display (exact\n"
            "values live in the pipeline tag; -D inverts exactly)"
        )
    )

    parser.add_argument(
        "-S", "--semitones",
        type=float,
        default=None,
        metavar="ST",
        help="Pitch-shift by ST semitones (alone: pitch-only;\nwith -s: speed+pitch combo like [1.2x 2.16st])"
    )

    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        default=False,
        help="Print output names, convert nothing"
    )

    parser.add_argument(
        "--autorename",
        type=str,
        nargs="?",
        const="",
        default="{artist} - {title} [Nightcore {diff}] [{quality}] [{resample}] [{denightcore}]",
        metavar="TEMPLATE",
        help="Rename output via a filename-stem template with bare\n"
             "{artist} {title} {diff} {quality} {resample} {denightcore}\n"
             "slots (bare flag keeps legacy 'Artist - Title [tags]';\n"
             "quality never rides fresh names, pipeline keeps it;\n"
             "default is '{artist} - {title} [Nightcore {diff}]\n"
             "[{quality}] [{resample}] [{denightcore}]' (empty slots\n"
             "drop their brackets; lossy outputs drop quality/rate);\n"
             "denightcore renders its verdict via {denightcore};\n"
             "bare flag goes last, or use --autorename=)"
    )

    parser.add_argument(
        "--no-delete-tags",
        action="store_true",
        default=False,
        help="With --tag-template: keep source tags not listed in the\n"
             "template (default: strip them, template only)"
    )

    parser.add_argument(
        "--no-stamp",
        action="store_true",
        default=False,
        help="Skip the 'Made with nightcore.py' DESCRIPTION provenance tag\n"
             "(default: every output carries it, overriding any template\n"
             "description; mapped source copies are scrubbed too)"
    )

    parser.add_argument(
        "--no-pipeline",
        action="store_true",
        default=False,
        help="Skip the encoder_settings pipeline tag (shift, resample,\n"
             "source fingerprint, tool versions; what --denightcore reads).\n"
             "Default writes it (mapped source copies are scrubbed too)"
    )

    parser.add_argument(
        "-o", "--output",
        type=str,
        default=None,
        metavar="FILE",
        help="Override the output filename (extension required, e.g.\n"
             "-o mix.mp3). Single-input runs only: --batch + -o is an error,\n"
             "as are several input files + -o"
    )

    parser.add_argument(
        "--output-folder",
        type=str,
        default=None,
        metavar="DIR",
        help="Base for batch folders (encodes/denightcore/renametag/\n"
             "std-samplerate). Existing DIR nests them (DIR/encodes/...);\n"
             "a missing DIR takes the folder's place (DIR/[codec]/).\n"
             "Single-file runs redirect their output there instead\n"
             "(several input files share it the same way)"
    )

    parser.add_argument(
        "--in-place",
        action="store_true",
        default=False,
        help="With -N: renametag alongside the inputs instead of\n"
             "copying to ./renametag/ (asks [y/N] first: originals move;\n"
             "cannot be combined with -o or --output-folder)"
    )

    parser.add_argument(
        "-N", "--renametag",
        action="store_true",
        default=False,
        help="Renametag mode: rewrite tags and rename via stream copy,\n"
             "no audio processing (copies land in ./renametag/, originals\n"
             "stay; --in-place moves alongside instead, -o names the copy\n"
             "verbatim). -F is ignored (container\n"
             "kept); -s/-S/-p/-r/-R/-D are errors. A stamp without\n"
             "Settings gains them backfilled from filename tags when\n"
             "present (stamps with Settings are preserved as-is)"
    )

    parser.add_argument(
        "--tag-template",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "Tag template file (key=\"value\" lines, multiline ok).\n"
            "Empty/bare values keep the source tag; tags missing\n"
            "from the file are stripped from outputs.\n"
            "title=/artist= values may use bare {title} {artist} {diff}\n"
            "{quality} {resample} {denightcore} slots (brackets are yours;\n"
            "{diff(N)} rounds for display, e.g. {diff(2)}), e.g.\n"
            "title=\"{title} [Nightcore {diff(2)}]\". An explicit title=\n"
            "is used verbatim; otherwise titles default to\n"
            "\"Title [Nightcore diff]\"."
        )
    )

    parser.add_argument(
        "--cover",
        type=str,
        default=None,
        metavar="IMG",
        help="PNG/JPG artwork: sole attached picture of outputs."
    )

    parser.add_argument(
        "--mkvid",
        action="store_true",
        default=False,
        help="Remux one song (+ optional .lrc lyrics, + image) into a\n"
        "1080p YouTube video: the image is letterboxed/pillarboxed into\n"
        "1920x1080 with black bars (never stretched) and the audio rides\n"
        "losslessly -- FLAC in mkv, ALAC in mp4 -- so YouTube's Opus\n"
        "transcode is one generation of loss. -F picks mkv (default)\n"
        "or mp4. No image and no embedded cover art is a loud error;\n"
        "no audio processing flags apply (it is a remux, not a conversion)."
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Print version and exit"
    )

    parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help="Check external tools (ffmpeg, sox, rubberband, encoders,\n"
             "tag readers, python libs), print versions/paths and exit"
    )

    parser.add_argument(
        "--pitch-diff",
        type=float,
        nargs="+",
        default=None,
        metavar="F",
        help=(
            "Calculator, no conversion: 1 speed or 2 frequencies\n"
            "-> semitones, cents, speed factor"
        )
    )

    parser.add_argument(
        "-D", "--denightcore",
        action="store_true",
        default=False,
        help=(
            "Undo from the stamp tag only (filenames are untrusted):\n"
            "inverts the stamped shift. Fails without a usable stamp.\n"
            "Names rebuild from tags as 'Artist - Title [quality]\n"
            "[denightcored BP/NBP]' (--autorename template honored).\n"
            "--tag-template is ignored; the stamp records the undo.\n"
            "BP needs a pure rate inversion (no pitch/resample/lossy)"
        )
    )

    parser.add_argument(
        "--speed-diff",
        type=float,
        default=None,
        metavar="ST",
        help="Calculator, no conversion: semitones -> speed factor"
    )

    # --lossy-opts takes values starting with "-" (encoder flags),
    # which the space form would parse as nightcore's own options
    # (-q:a 0 reads as -q). Only the = form is accepted; the space form
    # fails here instead of misparsing.
    for tok in sys.argv[1:]:
        if tok == "--lossy-opts":
            parser.error(
                f"{tok} requires the = form "
                f'(e.g. {tok}="-q:a 0"); space-separated values '
                f"starting with - would parse as nightcore flags")
    return parser.parse_args()


def print_summary(ctx):
    """End-of-run tally (default and -q; never -qq). One line: per-file
    detail already printed during the run, so it is not repeated."""
    if util._quiet >= 2:
        return
    conv = ctx.get("converted", 0)
    ret = ctx.get("renametagged", 0)
    skipped = ctx.get("skipped", 0) + ctx.get("skipped_notags", 0)
    failed = ctx.get("failed", [])
    if not conv and not ret and not skipped and not failed:
        return
    parts = []
    if conv:
        parts.append(f"{conv} converted")
    if ret:
        parts.append(f"{ret} renametagged")
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        parts.append(f"{len(failed)} failed")
    print(f"Summary: {', '.join(parts)}")


def finish_batch(ctx, files, failed, ok_message):
    """Shared batch tail: stash failures, report, summarize, exit code."""
    ctx["failed"] = failed
    if failed:
        print(f"Error: {len(failed)} of {len(files)} file(s) failed: "
              + ", ".join(failed), file=sys.stderr)
        print_summary(ctx)
        return 1
    print(ok_message)
    print_summary(ctx)
    return 0


def _stop_early(ctx, files, failed, done):
    """Graceful stop after one Ctrl+C: tally what finished, exit 130.

    The current file completed (nothing partial), so there is nothing
    to delete; unprocessed files are simply never started.
    """
    ctx["failed"] = failed
    print(f"Stopped after {done} of {len(files)} file(s) (interrupted).",
          file=sys.stderr)
    print_summary(ctx)
    return 130


def copy_batch_art(batch_dir, dest_dirs, dry_run, overwrite):
    """Copy cover art found in batch_dir into each dest dir.

    Same exists rule as audio (missing copies, -y refreshes);
    self-copies skipped. Returns copied (or would-copy) filenames.
    Never raises.
    """
    try:
        arts = sorted(p for p in batch_dir.iterdir()
                      if p.is_file() and p.stem.lower() in COVER_ART_NAMES
                      and p.suffix.lower() in COVER_ART_EXTS)
    except OSError:
        return []
    done = []
    for art in arts:
        for d in dest_dirs:
            dest = d / art.name
            try:
                if dest.resolve() == art.resolve():
                    continue
            except OSError:
                pass
            if dest.exists() and not overwrite:
                continue
            if dry_run:
                print(f"Would copy {art.name} -> {dest}")
            else:
                try:
                    shutil.copy2(art, dest)
                except OSError as e:
                    print(f"Warning: cannot copy art {art.name}: {e}",
                          file=sys.stderr)
                    continue
                say(f"  art: {art.name} -> {dest}")
            done.append(art.name)
    return done


def sweep_empty_dirs(args, batch_dir, dirs, dry_run):
    """Remove batch op dirs (plus their auto-made parent shells) left
    empty. Only empty dirs go: user paths (cwd, --output-folder, the
    batch dir itself) and pre-existing content are never touched."""
    if dry_run:
        return
    def _real(p):
        try:
            return Path(p).resolve()
        except OSError:
            return Path(p).absolute()

    keep = {_real(Path.cwd())}
    if args.output_folder is not None:
        keep.add(_real(args.output_folder))
    if batch_dir is not None:
        keep.add(_real(batch_dir))
    todo = []
    for d in dirs:
        d = Path(d)
        todo.append(d)
        if _real(d.parent) not in keep:
            todo.append(d.parent)
    for d in todo:
        try:
            d.rmdir()
        except OSError:
            pass


def main():
    signal.signal(signal.SIGINT, _on_sigint)
    util._enable_windows_vt()
    args = parse_arguments()
    util._quiet = args.quiet

    if args.check:
        return run_check()

    if args.pitch_diff is not None:
        vals = args.pitch_diff
        if len(vals) == 1:
            (factor,) = vals
            if factor <= 0 or not math.isfinite(factor):
                print("Error: --pitch-diff speed factor must be finite and > 0",
                      file=sys.stderr)
                return 1
            st = 12.0 * math.log2(factor)
            print(f"speed {factor!r}x = {st!r}st ({st * 100!r} cents)")
        elif len(vals) == 2:
            f1, f2 = vals
            if (f1 <= 0 or f2 <= 0
                    or not math.isfinite(f1) or not math.isfinite(f2)):
                print("Error: --pitch-diff frequencies must be finite and > 0",
                      file=sys.stderr)
                return 1
            st = 12.0 * math.log2(f2 / f1)
            print(f"{f2!r} Hz vs {f1!r} Hz: {st!r}st ({st * 100!r} cents), "
                  f"speed {f2 / f1!r}x")
        else:
            print("Error: --pitch-diff takes one speed factor or two frequencies",
                  file=sys.stderr)
            return 1
        return 0

    if args.speed_diff is not None:
        st = args.speed_diff
        if not math.isfinite(st):
            print("Error: --speed-diff semitones must be finite", file=sys.stderr)
            return 1
        print(f"{st!r}st ({st * 100!r} cents) = speed {speed_from_semitones(st)!r}x")
        return 0

    if args.recurse is not None and args.stats is None:
        print("Error: --recurse needs --stats "
              "(it only widens the folder report scan)", file=sys.stderr)
        return 1

    if args.stats is not None:
        # Read-only folder report: every other flag except --recurse
        # is a harmless passenger and is ignored (shell functions and
        # aliases carrying conversion flags keep working), so only the
        # recurse depth itself is validated.
        if args.recurse is not None and args.recurse != -1 \
                and args.recurse < 1:
            print("Error: --recurse needs a depth of 1 or more "
                  "(bare --recurse scans everything)", file=sys.stderr)
            return 1
        from .stats import run_stats
        if args.recurse is None:
            max_depth = 1
        elif args.recurse == -1:
            max_depth = None
        else:
            max_depth = args.recurse
        return run_stats(args.stats, max_depth=max_depth)


    cover_flag = None
    if args.cover:
        cover_path = Path(args.cover)
        if not cover_path.is_file():
            print(f"Error: cover image not found: {cover_path}", file=sys.stderr)
            return 1
        if cover_path.suffix.lower() not in _IMAGE_MIME:
            print(f"Error: --cover needs a PNG or JPG image: {cover_path}",
                  file=sys.stderr)
            return 1
        cover_flag = cover_path

    tmpl = None
    if args.tag_template:
        if not Path(args.tag_template).is_file():
            print(f"Error: tag template not found: {args.tag_template}",
                  file=sys.stderr)
            return 1
        try:
            tmpl = parse_tag_template(args.tag_template)
        except (OSError, ValueError) as e:
            print(f"Error: bad tag template: {e}", file=sys.stderr)
            return 1
        if not tmpl:
            print("Error: tag template has no valid entries", file=sys.stderr)
            return 1
    if args.no_delete_tags and tmpl is None:
        print("Error: --no-delete-tags needs --tag-template", file=sys.stderr)
        return 1

    missing = missing_tools("ffmpeg", "ffprobe")
    if missing:
        print(
            f"Error: required tools not found on PATH: {', '.join(missing)}.\n"
            "Install FFmpeg (provides ffmpeg + ffprobe) and re-run.",
            file=sys.stderr,
        )
        return 127

    if args.mkvid:
        from .mkvid import run_mkvid
        return run_mkvid(args)

    if args.format not in OUTPUT_FORMATS:
        print(
            f"Error: unknown output format {args.format!r} "
            "(flac, alac, mp3, opus, aac; mkv/mp4 require --mkvid)",
            file=sys.stderr,
        )
        return 1

    # -S alone (or with -p) is pitch-only; -s + -S together is one
    # combined speedup-then-pitch job: bit-perfect asetrate first, then
    # SoX upsamples by the semitone factor and rubberband (R3 defaults)
    # pitch-shifts.
    eff_semitones = args.semitones
    combo = eff_semitones is not None and args.speed != 1.0
    # st_signed is the semitone amount actually applied (reciprocal negates).
    if eff_semitones is None:
        st_signed = None
    elif args.reciprocal:
        st_signed = -eff_semitones
    else:
        st_signed = eff_semitones
    combo_seq = speed_from_semitones(st_signed) if combo else 1.0
    if eff_semitones is not None and not combo:
        speed = speed_from_semitones(eff_semitones)
    else:
        speed = args.speed

    if speed <= 0 or not math.isfinite(speed):
        print("Error: Speed factor must be a finite number > 0", file=sys.stderr)
        return 1

    if args.reciprocal:
        speed = 1 / speed

    if args.output is not None:
        if args.batch is not None or len(args.input) > 1:
            print("Error: -o/--output needs a single input "
                  "(no --batch, no several inputs: one name cannot fit "
                  "many files)",
                  file=sys.stderr)
            return 1
        if not Path(args.output).suffix:
            print("Error: -o/--output needs a file extension "
                  "(e.g. -o mix.mp3)", file=sys.stderr)
            return 1
    if args.in_place and not args.renametag:
        print("Error: --in-place needs -N/--renametag "
              "(only renametagging works in place)", file=sys.stderr)
        return 1
    if args.in_place and args.output_folder is not None:
        print("Error: --in-place cannot be combined with --output-folder "
              "(in place means alongside the inputs)", file=sys.stderr)
        return 1
    if args.in_place and args.output is not None:
        print("Error: --in-place cannot be combined with -o/--output "
              "(an explicit output is already a copy, nothing to move)",
              file=sys.stderr)
        return 1
    if args.output_folder is not None:
        od = Path(args.output_folder)
        if od.exists() and not od.is_dir():
            print(f"Error: --output-folder is not a directory: {od}",
                  file=sys.stderr)
            return 1

    lossy_opts = []
    if args.lossy_opts is not None:
        try:
            lossy_opts = shlex.split(args.lossy_opts)
        except ValueError as e:
            print(f"Error: could not parse --lossy-opts: {e}", file=sys.stderr)
            return 1
    is_lossy_format = bool(OUTPUT_FORMATS[args.format].get("lossy"))
    if lossy_opts and not is_lossy_format:
        print("Warning: --lossy-opts ignored for lossless output",
              file=sys.stderr)

    retags = args.renametag
    if retags:
        # Retagging rewrites tags+name only: anything that would change
        # audio is a loud error (a [1.2x] name on untouched audio lies).
        for flag, bad in (("-s/--speed", args.speed != 1.0),
                          ("-S/--semitones", args.semitones is not None),
                          ("-p/--pitch", args.pitch),
                          ("-r/--reciprocal", args.reciprocal),
                          ("-R/--resample", args.resample),
                          ("-D/--denightcore", args.denightcore)):
            if bad:
                print(f"Error: -N/--renametag cannot be combined with {flag} "
                      "(no audio is processed)", file=sys.stderr)
                return 1
        if args.format != "flac":
            print("Note: -N ignores -F (each file keeps its container)",
                  file=sys.stderr)
        if args.lossy_opts is not None:
            print("Warning: --lossy-opts ignored in renametag mode (no encoding)",
                  file=sys.stderr)

    if args.resample and is_lossy_format:
        print("Error: -R/--resample cannot be used with lossy output "
              "(lossy encoders manage rates themselves; resample a "
              "lossless intermediate first if you need a standard rate)",
              file=sys.stderr)
        return 1
    if args.denightcore and is_lossy_format:
        print("Error: -D/--denightcore needs a lossless output "
              "(re-encoding to lossy would lose quality over time)",
              file=sys.stderr)
        return 1

    # Lossy quality defaults (V0 / 128k / TVBR 91): applied only when the
    # user passes no --lossy-opts, which then replaces them wholesale
    # (appending would hand qaac duplicate -V flags, which it rejects).
    # ffmpeg AAC has no default worth using: it fails loudly instead.
    backend = None
    if is_lossy_format and not retags:
        try:
            dialect = detect_lossy_dialect(args.format, lossy_opts)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        backend = select_lossy_backend(args.format, dialect)
        if backend is None:
            return 127
        if not lossy_opts:
            default = LOSS_DEFAULTS[(args.format, backend["kind"])]
            if default is None:
                print("Error: ffmpeg AAC has no default (it isn't that "
                      "great); pass explicit --lossy-opts, e.g. "
                      "--lossy-opts=-b:a 160k (or better: install qaac)",
                      file=sys.stderr)
                return 1
            lossy_opts = list(default)
            say(f"Default lossy opts for {args.format} "
                f"via {backend['kind']}: {' '.join(default)}")

    ctx = {
        "tmpl": tmpl,
        "cover_flag": cover_flag,
        "lossy_opts": lossy_opts,
        "backend": backend,
        "no_stamp": args.no_stamp,
        "no_pipeline": args.no_pipeline,
        "speed": speed,
        "combo": combo,
        "st_signed": st_signed,
        "combo_seq": combo_seq,
        "eff_semitones": eff_semitones,
    }

    if args.batch is not None:
        if args.input:
            print("Error: --batch takes a directory, not file(s) "
                  "(drop the positional inputs)", file=sys.stderr)
            return 1
        batch_dir = Path(args.batch)
        if not batch_dir.is_dir():
            print(f"Error: not a directory: {batch_dir}", file=sys.stderr)
            return 1
        files = sorted(p for p in batch_dir.iterdir()
                       if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
        if not files:
            print(f"Error: no audio files in {batch_dir}", file=sys.stderr)
            return 1
        if retags:
            # Batch renametags copy to flat ./renametag/ (originals stay),
            # or --in-place rewrites alongside the inputs (originals
            # move: gated behind [y/N]). Lyrics files are not processed
            # directly: each follows its audio master as a sidecar.
            lyrics = [f for f in files if f.suffix.lower() == ".lrc"]
            if lyrics:
                print(f"Skipping {len(lyrics)} lyrics file(s) "
                      "(they follow their audio masters as sidecars)",
                      file=sys.stderr)
                ctx["skipped"] = ctx.get("skipped", 0) + len(lyrics)
                files = [f for f in files if f.suffix.lower() != ".lrc"]
                if not files:
                    print(f"Error: no audio files to renametag in {batch_dir}",
                          file=sys.stderr)
                    return 1
            in_place = args.in_place and not args.dry_run
            if in_place:
                try:
                    answer = input(
                        f"Renametag {len(files)} file(s) in place in "
                        f"{batch_dir}? Originals will be renamed and "
                        f"overwritten. [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                if answer != "y":
                    print("Aborted.", file=sys.stderr)
                    return 1
            op_base = None if args.in_place else batch_op_dir(args, "renametag")
            if op_base is not None and not args.dry_run:
                op_base.mkdir(parents=True, exist_ok=True)
            ctx["batch_total"] = len(files)
            failed = []
            for bi, candidate in enumerate(files):
                if util._stop_requested:
                    return _stop_early(ctx, files, failed, bi)
                ctx["batch_index"] = bi
                if run_renametag(args, candidate, op_base, ctx) != 0:
                    failed.append(candidate.name)
            ctx["failed"] = failed
            if op_base is not None:
                copy_batch_art(batch_dir, [op_base], args.dry_run,
                               args.overwrite)
                sweep_empty_dirs(args, batch_dir, [op_base],
                                 args.dry_run)
            verb = "Would renametag" if args.dry_run else "Renametagged"
            dest = (f"in place in {batch_dir}" if op_base is None
                    else f"-> {op_base}")
            skipped = ctx.get("skipped", 0)
            note = f" ({skipped} skipped)" if skipped else ""
            return finish_batch(
                ctx, files, failed,
                f"{verb}: {len(files)} file(s){note} {dest}")
        if args.resample:
            # -R means standard-rate-only output: everything lands in
            # std-samplerate/[format]/, no weird-rate copies (batch or
            # single). Lossless output is enforced in main.
            out_base = batch_op_dir(args, "std-samplerate", args.format)
        elif args.denightcore:
            out_base = batch_op_dir(args, "denightcore", args.format)
        else:
            out_base = batch_op_dir(args, "encodes", args.format)
        if not args.dry_run:
            out_base.mkdir(parents=True, exist_ok=True)
        ctx["batch_total"] = len(files)
        failed = []
        for bi, candidate in enumerate(files):
            if util._stop_requested:
                return _stop_early(ctx, files, failed, bi)
            ctx["batch_index"] = bi
            if run_conversion(args, candidate, out_base, ctx) != 0:
                failed.append(candidate.name)
        ctx["failed"] = failed
        copy_batch_art(batch_dir, [out_base], args.dry_run,
                       args.overwrite)
        sweep_empty_dirs(args, batch_dir, [out_base], args.dry_run)
        verb = "Would convert" if args.dry_run else "Done"
        skipped = ctx.get("skipped", 0) + ctx.get("skipped_notags", 0)
        note = f" ({skipped} skipped)" if skipped else ""
        return finish_batch(
            ctx, files, failed,
            f"{verb}: {len(files)} file(s){note} -> {out_base}")
    if not args.input:
        print("Error: input file is required", file=sys.stderr)
        return 1
    inputs = [Path(p) for p in args.input]
    if len(inputs) == 1:
        single = inputs[0]
        if retags:
            # Single-file retags copy to ./renametag/ like batch (originals
            # stay); --in-place moves alongside instead (asks [y/N] first),
            # -o names the copy verbatim.
            op_base = None
            if args.output is None:
                if args.in_place and not args.dry_run:
                    try:
                        answer = input(
                            f"Renametag {single.name} in place? "
                            f"Original will be renamed and overwritten. "
                            f"[y/N] ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        answer = ""
                    if answer != "y":
                        print("Aborted.", file=sys.stderr)
                        return 1
                op_base = (None if args.in_place else
                           batch_op_dir(args, "renametag"))
                if op_base is not None and not args.dry_run:
                    op_base.mkdir(parents=True, exist_ok=True)
            rc = run_renametag(args, single, op_base, ctx)
        else:
            rc = run_conversion(args, single, None, ctx)
        print_summary(ctx)
        return rc
    # Several input files (audio and/or .lrc lyrics) share one settings
    # run: same flags apply to each, no for-loop needed. Outputs land
    # in the current directory (or redirected via --output-folder);
    # -o cannot name them all.
    if retags:
        if args.output is not None:
            print("Error: -o/--output needs a single input "
                  "(one name cannot fit many files)", file=sys.stderr)
            return 1
        if args.in_place and not args.dry_run:
            try:
                answer = input(
                    f"Renametag {len(inputs)} file(s) in place? "
                    f"Originals will be renamed and overwritten. "
                    f"[y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer != "y":
                print("Aborted.", file=sys.stderr)
                return 1
        op_base = (None if args.in_place
                   else batch_op_dir(args, "renametag"))
        if op_base is not None and not args.dry_run:
            op_base.mkdir(parents=True, exist_ok=True)
        lyrics = [f for f in inputs if f.suffix.lower() == ".lrc"]
        if lyrics:
            print(f"Skipping {len(lyrics)} lyrics file(s) "
                  "(they follow their audio masters as sidecars)",
                  file=sys.stderr)
            ctx["skipped"] = ctx.get("skipped", 0) + len(lyrics)
            inputs = [f for f in inputs if f.suffix.lower() != ".lrc"]
            if not inputs:
                print("Error: no audio files to renametag", file=sys.stderr)
                return 1
        ctx["batch_total"] = len(inputs)
        failed = []
        for bi, candidate in enumerate(inputs):
            if util._stop_requested:
                return _stop_early(ctx, inputs, failed, bi)
            ctx["batch_index"] = bi
            if run_renametag(args, candidate, op_base, ctx) != 0:
                failed.append(candidate.name)
        verb = "Would renametag" if args.dry_run else "Renametagged"
        dest = (f"in place ({len(inputs)} file(s))"
                if op_base is None else f"-> {op_base}")
        skipped = ctx.get("skipped", 0)
        note = f" ({skipped} skipped)" if skipped else ""
        return finish_batch(
            ctx, inputs, failed,
            f"{verb}: {len(inputs)} file(s){note} {dest}")
    ctx["batch_total"] = len(inputs)
    failed = []
    for bi, candidate in enumerate(inputs):
        if util._stop_requested:
            return _stop_early(ctx, inputs, failed, bi)
        ctx["batch_index"] = bi
        if run_conversion(args, candidate, None, ctx) != 0:
            failed.append(candidate.name)
    verb = "Would convert" if args.dry_run else "Done"
    skipped = ctx.get("skipped", 0) + ctx.get("skipped_notags", 0)
    note = f" ({skipped} skipped)" if skipped else ""
    if args.output_folder is not None:
        dest = f"-> {Path(args.output_folder)}"
    else:
        dest = "in the current directory"
    return finish_batch(
        ctx, inputs, failed,
        f"{verb}: {len(inputs)} file(s){note} {dest}")
