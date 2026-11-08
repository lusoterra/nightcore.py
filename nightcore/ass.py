"""ASS subtitle retiming (sidecar .ass files, anime style).

Dialogue timestamps are [Hours:]MM:SS.cc start/end fields; scaling
them by 1/speed keeps the subtitle track synced to sped video.
Embedded subtitles (any codec) are retimed in-place by the video
muxer via the setts bitstream filter, not by this module.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

from .naming import single_parent
from .util import _active_outputs, batch_prefix, say

# Dialogue: layer, start, end, ...  start/end are H:MM:SS.cc (ASS uses
# centiseconds for .cc, hours may be omitted). Other line kinds
# (Style, V4+ Styles, Comment) pass through verbatim.
_ASS_TIME = re.compile(
    r"^(\d+):(\d{2}):(\d{2})\.(\d{2})$")


def _parse_ass_time(text):
    m = _ASS_TIME.match(text.strip())
    if not m:
        # Try without hours: MM:SS.cc
        m2 = re.match(r"^(\d{2}):(\d{2})\.(\d{2})$", text.strip())
        if not m2:
            return None
        mm, ss, cc = [int(g) for g in m2.groups()]
        return ((mm * 60 + ss) * 1000 + cc * 10)
    hh, mm, ss, cc = [int(g) for g in m.groups()]
    return (((hh * 60 + mm) * 60 + ss) * 1000 + cc * 10)


def _format_ass_time(total_ms):
    total_ms = max(0, int(total_ms))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms // 10:02d}"


def retime_ass(text, speed):
    """Scale every Dialogue start/end by 1/speed; return (new_text, count)."""
    count = 0
    out = []
    for line in text.splitlines():
        if line.startswith("Dialogue:"):
            body = line[len("Dialogue:"):]
            parts = body.split(",", 3)
            # Dialogue: layer, start, end, rest
            if len(parts) == 4:
                layer, start, end, rest = parts
                s_ms = _parse_ass_time(start)
                e_ms = _parse_ass_time(end)
                if s_ms is not None and e_ms is not None:
                    count += 1
                    new_start = _format_ass_time(int(s_ms / speed))
                    new_end = _format_ass_time(int(e_ms / speed))
                    line = f"Dialogue:{layer},{new_start},{new_end},{rest}"
        out.append(line)
    return "\n".join(out), count


def run_ass(args, input_path, out_dir, ctx):
    """Retime a sidecar .ass file (like run_lrc). Returns exit code."""
    speed = ctx["speed"]
    if args.denightcore:
        print("Error: --denightcore is not supported for .ass subtitles "
              "(use -s)", file=sys.stderr)
        return 1
    if args.pitch or ctx.get("eff_semitones") is not None:
        print("Error: -p/-S pitch shifting is not supported for .ass "
              "subtitles (only -s retiming)", file=sys.stderr)
        return 1
    output_path = ((out_dir if out_dir is not None
                    else single_parent(args, input_path))
                   / (Path(input_path).stem + ".ass"))
    # Mirror the same-stem video's speed-suffixed name when retiming.
    if speed != 1.0:
        output_path = output_path.with_name(
            f"{output_path.stem} [{speed:g}x]{output_path.suffix}")
    head = batch_prefix(ctx)
    if output_path == input_path:
        print("Error: output would overwrite input, aborting", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"{head}{output_path}")
        return 0
    if output_path.exists() and not args.overwrite:
        print(f"Error: output already exists: {output_path} (use -y to overwrite)",
              file=sys.stderr)
        return 1
    try:
        raw = input_path.read_bytes()
    except OSError as e:
        print(f"Error: cannot read {input_path}: {e}", file=sys.stderr)
        return 1
    try:
        text = raw.decode("utf-8-sig")
        enc = "utf-8"
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
        enc = "latin-1"
    new_text, count = retime_ass(text, speed)
    if count == 0:
        print(f"Warning: no Dialogue timestamps found in {input_path.name}",
              file=sys.stderr)
    data = new_text.encode(enc)
    if text.endswith("\n"):
        data += b"\n"
    say(f"{head}{output_path.name}: retime subtitles {speed:g}x "
        f"({count} dialogues)")
    _active_outputs.add(output_path)
    tmpname = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=str(output_path.parent),
                prefix=".nightcore-", suffix=".ass",
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
