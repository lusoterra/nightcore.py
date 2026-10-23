"""LRC lyric retiming."""

from pathlib import Path
import sys
import tempfile

from .constants import AUDIO_EXTS, _LOSSLESS_EXTS, _LRC_LENGTH, _LRC_STAMP
from .naming import claim_output_path, single_parent
from .plan import predict_audio_stem
from .tags import sanitize_filename
from .util import _active_outputs, batch_prefix, say

def _lrc_frac_to_ms(frac):
    """Fractional LRC digits to milliseconds (None -> 0).

    One digit is tenths of a second, two are centiseconds, three are
    milliseconds -- the shapes found in the wild.
    """
    if frac is None:
        return 0
    if len(frac) == 1:
        return int(frac) * 100
    if len(frac) == 2:
        return int(frac) * 10
    return int(frac[:3])


def _lrc_scale_ms(total_ms, speed):
    """Scale milliseconds by 1/speed; round half up to centiseconds."""
    return (int(total_ms / speed) + 5) // 10 * 10


def _lrc_format(total_ms):
    """Format milliseconds as a canonical [mm:ss.cc] timestamp."""
    total_ms = max(0, int(total_ms))
    mm, rem = divmod(total_ms, 60000)
    ss, ms = divmod(rem, 1000)
    return f"[{mm:02d}:{ss:02d}.{ms // 10:02d}]"


def retime_lrc(text, speed):
    """Scale every LRC timestamp by 1/speed; return (new_text, count).

    All [mm:ss.cc] lyric tags and the [length:] header move; metadata
    ([ar:], [al:], [ti:], [au:], [offset:], ...) passes through
    verbatim. Output is normalized to [mm:ss.cc] timestamps. count is
    the number of lyric timestamps scaled (length excluded).
    """
    count = 0

    def _scale(m):
        nonlocal count
        count += 1
        total = (int(m.group(1)) * 60000 + int(m.group(2)) * 1000
                 + _lrc_frac_to_ms(m.group(3)))
        return _lrc_format(_lrc_scale_ms(total, speed))

    def _length(m):
        total = (int(m.group(2)) * 60000 + int(m.group(3)) * 1000
                 + _lrc_frac_to_ms(m.group(4)))
        scaled = _lrc_scale_ms(total, speed)
        mm, rem = divmod(max(0, scaled), 60000)
        ss, ms = divmod(rem, 1000)
        return (f"{m.group(1)}[length:{mm:02d}:{ss:02d}.{ms // 10:02d}]"
                f"{m.group(5)}")

    out = []
    for line in text.splitlines():
        if _LRC_LENGTH.match(line):
            out.append(_LRC_LENGTH.sub(_length, line))
        else:
            out.append(_LRC_STAMP.sub(_scale, line))
    return "\n".join(out), count


def find_parent_audio(lrc_path):
    """Same-stem audio file next to .lrc lyrics, or None.

    Masters (lossless) win over other copies; ties break alphabetically.
    Never raises (missing/unreadable directory -> None).
    """
    try:
        entries = list(lrc_path.parent.iterdir())
    except OSError:
        return None
    cands = [p for p in entries
             if p.is_file() and p.stem == lrc_path.stem
             and p.suffix.lower() in AUDIO_EXTS
             and p.suffix.lower() != ".lrc"]
    cands.sort(key=lambda p: (0 if p.suffix.lower() in _LOSSLESS_EXTS else 1,
                              p.name))
    return cands[0] if cands else None


def run_lrc(args, input_path, out_dir, ctx):
    """Retime .lrc lyrics (or copy them at speed 1), in the output directory.

    With -s the timestamps scale by 1/speed; at speed 1 (plain
    transcoding, e.g. a --batch sharing run) the file is copied
    byte-identical. Either way the output mirrors the parent audio's
    output name (same stem, .lrc), falling back to "stem [Nx].lrc" --
    or the bare stem at speed 1 -- without a same-stem parent file.
    Only -s (and -r, which folds into it) changes lyrics; -D/-p/-S are
    rejected loudly instead of silently ignored.
    Returns a process-style exit code.
    """
    speed = ctx["speed"]
    eff_semitones = ctx["eff_semitones"]
    if args.denightcore:
        print("Error: --denightcore is not supported for .lrc lyrics "
              "(use -s)", file=sys.stderr)
        return 1
    if args.pitch or eff_semitones is not None:
        print("Error: -p/-S pitch shifting is not supported for .lrc "
              "lyrics (only -s retiming)", file=sys.stderr)
        return 1
    # Everything else (-F, --tag-template, -R, --cover,
    # --lossy-opts) is accepted for naming fidelity: the
    # lyrics mirror the parent audio's output name, which those flags
    # shape. They change nothing about the lyrics themselves.
    copy_only = speed == 1.0
    # Fast skip for batch reruns: mirror the parent audio's output name
    # with no probing (same fast predictor as audio jobs). Hits return
    # here; misses fall through to the full predict below.
    if (out_dir is not None and not args.overwrite and not args.dry_run
            and ctx.get("existing_outputs") is not None):
        from .plan import fast_default_stem as _fast_stem
        _parent = None
        _bfiles = ctx.get("batch_files")
        if _bfiles is not None:
            _cands = [Path(p) for p in _bfiles
                      if Path(p).stem == input_path.stem
                      and Path(p).suffix.lower() in AUDIO_EXTS
                      and Path(p).suffix.lower() != ".lrc"]
            _cands.sort(key=lambda p: (
                0 if p.suffix.lower() in _LOSSLESS_EXTS else 1, p.name))
            _parent = _cands[0] if _cands else None
        else:
            _parent = find_parent_audio(input_path)
        try:
            if _parent is not None:
                _ps = _fast_stem(args, ctx, _parent)
            else:
                _ps = (sanitize_filename(input_path.stem) if copy_only
                       else sanitize_filename(
                           f"{input_path.stem} [{speed!r}x]"))
        except Exception:
            _ps = None
        if _ps is not None:
            _raw = out_dir / (_ps + ".lrc")
            _claimed = ctx.setdefault("claimed_outputs", set())
            _cand = _raw
            _n = 2
            while _cand in _claimed:
                _cand = _raw.with_name(
                    f"{_raw.stem} ({_n}){_raw.suffix}")
                _n += 1
            if (_cand.name in ctx["existing_outputs"]
                    or _cand.exists()):
                _claimed.add(_cand)
                print(f"Skipping {input_path.name} (output exists, "
                      "use -y to re-encode)", file=sys.stderr)
                ctx["skipped"] = ctx.get("skipped", 0) + 1
                return 0
            # Miss: fall through without claiming (the full path below
            # claims exactly once).
    stem = predict_audio_stem(args, ctx, find_parent_audio(input_path))
    if stem is None:
        # No same-stem parent audio (or unreadable): fall back to the
        # input stem, shift-suffixed when retiming.
        stem = (sanitize_filename(input_path.stem) if copy_only else
                sanitize_filename(f"{input_path.stem} [{speed!r}x]"))
    output_path = ((out_dir if out_dir is not None
                    else single_parent(args, input_path))
                   / (stem + ".lrc"))
    head = batch_prefix(ctx)
    if output_path == input_path:
        print("Error: output would overwrite input, aborting", file=sys.stderr)
        return 1
    if out_dir is not None or ctx.get("batch_total", 0) > 1:
        # Same collision guard as audio outputs: several inputs (or a
        # batch folder) funnelling into one directory must not overwrite
        # each other when names collide.
        output_path = claim_output_path(ctx, output_path)
        if output_path == input_path:
            print("Error: output would overwrite input, aborting",
                  file=sys.stderr)
            return 1
    if args.dry_run:
        print(f"{head}{output_path}")
        return 0
    if output_path.exists() and not args.overwrite:
        if out_dir is not None:
            print(f"Skipping {input_path.name} (output exists, "
                  "use -y to re-encode)", file=sys.stderr)
            ctx["skipped"] = ctx.get("skipped", 0) + 1
            return 0
        print(
            f"Error: output already exists: {output_path} (use -y to overwrite)",
            file=sys.stderr,
        )
        return 1

    if copy_only:
        # Already timed right (speed-1 transcode run): byte-identical
        # copy under the mirrored name.
        try:
            raw = input_path.read_bytes()
        except OSError as e:
            print(f"Error: cannot read {input_path}: {e}", file=sys.stderr)
            return 1
        say(f"{head}{output_path.name}: copy lyrics (already timed)")
        _active_outputs.add(output_path)
        tmpname = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="wb", dir=str(output_path.parent),
                    prefix=".nightcore-", suffix=".lrc",
                    delete=False) as f:
                f.write(raw)
                tmpname = f.name
            Path(tmpname).replace(output_path)
        except OSError as e:
            if tmpname is not None:
                Path(tmpname).unlink(missing_ok=True)
            _active_outputs.discard(output_path)
            print(f"Error: cannot write {output_path}: {e}", file=sys.stderr)
            return 1
        _active_outputs.discard(output_path)
        ctx["converted"] = ctx.get("converted", 0) + 1
        return 0

    try:
        raw = input_path.read_bytes()
    except OSError as e:
        print(f"Error: cannot read {input_path}: {e}", file=sys.stderr)
        return 1
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
        enc = "utf-8"
    except UnicodeDecodeError:
        # Not UTF-8: latin-1 passes every byte through exactly, so the
        # non-timestamp bytes survive the round trip untouched.
        text = raw.decode("latin-1")
        enc = "latin-1"
    new_text, count = retime_lrc(text, speed)
    if count == 0:
        print(f"Warning: no timestamps found in {input_path.name}",
              file=sys.stderr)
    out = new_text
    if text.endswith(("\n", "\r")):
        out += "\n"
    # (A BOM on non-UTF-8 input stays inside the latin-1 text verbatim.)
    keep_bom = has_bom and enc == "utf-8"
    data = (b"\xef\xbb\xbf" if keep_bom else b"") + out.encode(enc)
    noun = "timestamp" if count == 1 else "timestamps"
    say(f"{head}{output_path.name}: retime lyrics {speed!r}x "
        f"({count} {noun})")
    _active_outputs.add(output_path)
    tmpname = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=str(output_path.parent),
                prefix=".nightcore-", suffix=".lrc",
                delete=False) as f:
            f.write(data)
            tmpname = f.name
        Path(tmpname).replace(output_path)
    except OSError as e:
        if tmpname is not None:
            Path(tmpname).unlink(missing_ok=True)
        _active_outputs.discard(output_path)
        print(f"Error: cannot write {output_path}: {e}", file=sys.stderr)
        return 1
    _active_outputs.discard(output_path)
    ctx["converted"] = ctx.get("converted", 0) + 1
    return 0
