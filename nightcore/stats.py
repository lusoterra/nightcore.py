"""Read-only folder statistics (--stats)."""

from collections import Counter
from concurrent import futures
from datetime import datetime
from pathlib import Path
import os
import shutil
import statistics
import sys

from . import util
from .audio import format_rate, get_audio_info, get_sample_rate, mp3_header_info, probe_audio
from .constants import AUDIO_EXTS, LOSSY_CODECS
from .naming import recover_filename_quality, split_shift_tags
from .tags import pipeline_has, pipeline_origin, read_shift_source, short_num
from .util import missing_tools


def _term_width():
    """Columns of the stderr terminal (80 fallback).

    Measured on stderr itself: that is where the progress line
    draws, and stdout may be wider, narrower, or redirected.
    Overshooting the real width wraps the line, and wrapped
    fragments survive the carriage-return erase as residue.
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


def _progress_tick(done, total, name, last_len):
    """Rewrite the one-line stderr progress; TTYs only, returns width.

    Carriage-return overwrite, never a newline, so the finished report
    stays clean. Anything redirected (not a TTY) or -qq stays silent.
    The line never exceeds the stderr terminal width (names are
    whitespace-collapsed and tail-truncated), so nothing wraps and the
    final clear erases it exactly. Returns the widest width so far.
    """
    if util._quiet >= 2 or not sys.stderr.isatty():
        return 0
    prefix = f"Stats [{done:0{len(str(total))}d}/{total}] "
    width = _term_width()
    room = max(10, width - len(prefix))
    shown = " ".join(str(name).split())
    if len(shown) > room:
        # Keep the tail: "[1.2x]" suffixes tell files apart.
        shown = "..." + shown[-(room - 3):]
    msg = (prefix + shown)[:width]
    sys.stderr.write("\r" + msg + " " * max(0, last_len - len(msg)))
    sys.stderr.flush()
    return max(last_len, len(msg))


def _progress_clear(last_len):
    """Erase the progress line (TTYs only)."""
    if last_len and util._quiet < 2 and sys.stderr.isatty():
        sys.stderr.write("\r" + " " * last_len + "\r")
        sys.stderr.flush()


def _norm_codec(codec):
    """Lowercase underscore-form codec name, or None."""
    if codec is None:
        return None
    return str(codec).lower().replace("-", "_")


def _is_lossy_codec(codec):
    return _norm_codec(codec) in LOSSY_CODECS


def is_truly_lossless(probe_codec, origin_codec=None, filename_codec=None):
    """Strict lossless verdict for one file.

    Truly lossless means a lossless container now AND no lossy origin
    anywhere: the probe codec, the pipeline source-codec (when stamped)
    and the filename quality tag (for old/foreign files) must all be
    clean. A FLAC wrapping an MP3 probes as flac but fails here.
    """
    if _is_lossy_codec(probe_codec):
        return False
    if origin_codec is not None and _is_lossy_codec(origin_codec):
        return False
    if filename_codec is not None and _is_lossy_codec(filename_codec):
        return False
    return True


def speed_of(path):
    """(speed, semitones, source) for one file.

    Reads the trusted pipeline tag first (what -D uses), falling back
    to filename tags ([1.2x]) for old or foreign files. source is
    "stamp", "filename", "denightcore" (restored: counts as 1.0x) or
    "none" (unstamped: assumed 1.0x). Never raises.
    """
    try:
        parsed, raw = read_shift_source(path)
    except (OSError, ValueError):
        parsed, raw = None, ""
    if parsed is not None:
        return parsed[0], parsed[1], "stamp", raw
    if raw and pipeline_has(raw, "denightcore"):
        return 1.0, 0.0, "denightcore", raw
    try:
        _, speed, st, found, _ = split_shift_tags(path.stem)
    except (OSError, ValueError):
        found, speed, st = False, 1.0, 0.0
    if found:
        return speed, st, "filename", raw or ""
    return 1.0, 0.0, "none", raw or ""


def _fmt_speed(speed):
    return f"{short_num(round(speed, 6))}x"


def _fmt_st(st):
    return f"{short_num(round(st, 6))}st"


def _top(counter, limit=10):
    """Counter items sorted by count desc, then key asc; capped."""
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def _gather(root, max_depth):
    """[(dirpath, [files])] breadth-first, root itself is depth 1.

    max_depth None scans everything; symlinked dirs are never
    descended into (no cycle risk). Unreadable dirs are skipped.
    Order is deterministic (sorted entries, level by level).
    """
    root = Path(root)
    levels = [(root, 1)]
    out = []
    idx = 0
    while idx < len(levels):
        d, depth = levels[idx]
        idx += 1
        try:
            entries = sorted(p for p in d.iterdir())
        except OSError:
            continue
        out.append((d, [p for p in entries if p.is_file()]))
        if max_depth is None or depth < max_depth:
            levels.extend((p, depth + 1) for p in entries
                          if p.is_dir() and not p.is_symlink())
    return out


def _collect_one(path):
    """("ok", record) or ("bad", name) for one audio file. Never raises."""
    try:
        probe = probe_audio(path)
    except (OSError, ValueError):
        return "bad", path.name
    if not probe.get("audio"):
        return "bad", path.name
    try:
        codec, bit_rate, bits, fmt_rate, _ = get_audio_info(path)
        sample_rate = get_sample_rate(path)
        if not sample_rate:
            # ffprobe sniffs garbage as a phantom stream (rate 0):
            # not real audio.
            return "bad", path.name
        if codec == "mp3":
            # Stream bit_rate on VBR MP3s is unreliable; use the
            # file average like the encoder path does.
            mode, _ = mp3_header_info(path)
            if mode == "VBR" and fmt_rate:
                bit_rate = fmt_rate
        speed, st, source, raw = speed_of(path)
        origin = pipeline_origin(raw or "")
        forge = recover_filename_quality(path.stem)
        origin_codec = _norm_codec(origin["codec"])
        filename_codec = _norm_codec(forge["codec"])
        return "ok", {
            "name": path.name,
            "codec": _norm_codec(codec),
            "bit_rate": bit_rate,
            "bits": bits,
            "sample_rate": sample_rate,
            "speed": speed,
            "st": st,
            "shift_source": source,
            "origin_codec": origin_codec,
            "filename_codec": filename_codec,
            "lossless": is_truly_lossless(codec, origin_codec,
                                          filename_codec),
            "resampled": bool(raw and pipeline_has(raw, "resample")),
            "denightcored": bool(raw and pipeline_has(raw,
                                                       "denightcore")),
        }
    except (OSError, ValueError):
        return "bad", path.name


def _bar(count, peak, width=16):
    """Block bar scaled to the top count ("" when empty)."""
    if not peak or not count:
        return ""
    return "█" * max(1, round(count / peak * width))


def collect_stats(stats_dir, progress=False, max_depth=1):
    """Gather per-file facts under stats_dir.

    max_depth caps directory levels (the folder itself is 1, i.e.
    depth 1 is today's flat scan); None scans everything. Lyrics
    pair per directory: a same-stem .lrc only counts for audio next
    to it. Returns {"dir", "max_depth", "audio": [...records...],
    "lrc_files": [...], "unreadable": [...names...]}; each record
    carries "d" (relative dir, "." at top) and "lyrics". Never raises
    (unreadable files land in "unreadable"). progress=True shows a
    self-erasing one-line scan indicator on stderr (TTYs only:
    redirected output stays clean).
    """
    stats_dir = Path(stats_dir)
    try:
        levels = _gather(stats_dir, max_depth)
    except OSError:
        levels = []
    audio_paths = []
    lrc_by_dir = {}
    lrc_files = []
    for d, files in levels:
        try:
            rel = str(d.relative_to(stats_dir))
        except ValueError:
            rel = str(d)
        stems = set()
        for p in files:
            if p.suffix.lower() == ".lrc":
                stems.add(p.stem)
                lrc_files.append(p)
            elif p.suffix.lower() in AUDIO_EXTS:
                audio_paths.append((rel, p))
        lrc_by_dir[rel] = stems
    audio = []
    unreadable = []
    total = len(audio_paths)
    # Probing shells out per file (ffprobe, metaflac, opusinfo... are
    # all single-input tools with no batch mode), so files are probed
    # across a thread pool; map() keeps the record order deterministic.
    last_len = 0
    with futures.ThreadPoolExecutor(
            max_workers=min(16, total) or 1) as ex:
        jobs = (ex.submit(_collect_one, p) for _, p in audio_paths)
        for done, ((rel, p), job) in enumerate(zip(audio_paths, jobs), 1):
            if progress:
                last_len = _progress_tick(done, total, p.name, last_len)
            kind, payload = job.result()
            if kind == "ok":
                payload["d"] = rel
                payload["lyrics"] = p.stem in lrc_by_dir.get(rel, set())
                audio.append(payload)
            else:
                unreadable.append(payload)
    if progress:
        _progress_clear(last_len)
    return {"dir": stats_dir, "max_depth": max_depth, "audio": audio,
            "lrc_files": lrc_files, "unreadable": unreadable}


def render_stats(data):
    """Human-readable report lines for collect_stats() output."""
    audio = data["audio"]
    n = len(audio)
    lines = []
    max_depth = data.get("max_depth", 1)
    if max_depth == 1:
        lines.append(f"Stats for {data['dir']} (depth 1): "
                     f"{n} audio file(s), "
                     f"{len(data['lrc_files'])} .lrc file(s)")
    else:
        scope = "everything" if max_depth is None else f"depth <= {max_depth}"
        ndirs = len({r["d"] for r in audio}) if n else 0
        lines.append(f"Stats for {data['dir']} (recursive, {scope}): "
                     f"{n} audio file(s) in {ndirs} dir(s), "
                     f"{len(data['lrc_files'])} .lrc file(s)")
    if not n:
        return lines

    with_lyrics = sum(1 for r in audio if r.get("lyrics"))
    lines.append(f"Lyrics: {with_lyrics} of {n} audio file(s) have "
                 f"same-stem .lrc ({with_lyrics * 100 // n}%)")

    speeds = [r["speed"] for r in audio]
    avg = statistics.fmean(speeds)
    med = statistics.median(speeds)
    lines.append(f"Speed: average {_fmt_speed(avg)}, median "
                 f"{_fmt_speed(med)} across {n} file(s) "
                 f"(pitch-only counts as 1.0x)")
    dist = Counter(_fmt_speed(round(s, 6)) for s in speeds)
    peak = max(dist.values())
    rows = _top(dist)
    key_w = max(len(k) for k, _ in rows)
    count_w = max(len(str(v)) for _, v in rows)
    for key, count in rows:
        lines.append(f"  {key:<{key_w}}: {count:>{count_w}} "
                     f"{_bar(count, peak)}")
    if len(dist) > 10:
        lines.append(f"  ... and {len(dist) - 10} more")

    src = Counter(r["shift_source"] for r in audio)
    lines.append(f"Shift source: {src.get('stamp', 0)} stamped, "
                 f"{src.get('filename', 0)} filename, "
                 f"{src.get('none', 0)} unstamped (assumed 1.0x)"
                 + (f", {src.get('denightcore', 0)} denightcored"
                    if src.get("denightcore") else ""))

    pitched = [r for r in audio if r["st"] != 0.0]
    if pitched:
        std = Counter(_fmt_st(round(r["st"], 6)) for r in pitched)
        top_st = ", ".join(f"{k} x{v}"
                           for k, v in _top(std, 5))
        lines.append(f"Pitch: {len(pitched)} file(s) with semitone shift "
                     f"(most common: {top_st})")
    else:
        lines.append("Pitch: none")

    lossless = sum(1 for r in audio if r["lossless"])
    lossy_now = sum(1 for r in audio if _is_lossy_codec(r["codec"]))
    lossy_origin = sum(1 for r in audio
                       if not r["lossless"] and not _is_lossy_codec(r["codec"]))
    lines.append(f"Quality: {lossless} truly lossless "
                 f"({lossless * 100 // n}%), "
                 f"{lossy_origin} lossless-container lossy-origin, "
                 f"{lossy_now} lossy")
    containers = Counter((r["codec"] or "?").replace("_", "-")
                         for r in audio)
    lines.append("Containers: " + ", ".join(
        f"{k}: {v}" for k, v in _top(containers)))
    origins = Counter((r["origin_codec"] or r["filename_codec"]
                       or r["codec"] or "?").replace("_", "-")
                      for r in audio)
    lines.append("Source codecs: " + ", ".join(
        f"{k}: {v}" for k, v in _top(origins)))

    rated = [(r["codec"], r["bit_rate"]) for r in audio
             if r["bit_rate"]]
    if rated:
        avg_k = statistics.fmean(b for _, b in rated) / 1000
        by_codec = {}
        for codec, rate in rated:
            by_codec.setdefault(codec or "?", []).append(rate)
        parts = []
        for codec, rates in sorted(by_codec.items()):
            parts.append(f"{codec.replace('_', '-')}: "
                         f"{statistics.fmean(rates) / 1000:.0f}k "
                         f"x{len(rates)}")
        lines.append(f"Bitrate: average {avg_k:.0f}k across {len(rated)} "
                     f"file(s) ({', '.join(parts)})")
    else:
        lines.append("Bitrate: unknown")
    depths = Counter(f"{r['bits']}-bit" if r["bits"] else "unknown"
                     for r in audio)
    lines.append("Depth: " + ", ".join(
        f"{k}: {v}" for k, v in _top(depths)))
    rates = Counter(format_rate(r["sample_rate"]) for r in audio
                    if r["sample_rate"])
    if rates:
        lines.append("Sample rates: " + ", ".join(
            f"{k}: {v}" for k, v in _top(rates, 5)))
    resampled = sum(1 for r in audio if r["resampled"])
    denight = sum(1 for r in audio if r["denightcored"])
    lines.append(f"Resampled: {resampled} file(s) with resample history; "
                 f"denightcored: {denight}")
    if max_depth != 1:
        by_dir = Counter(r["d"] for r in audio)
        top_dirs = _top(by_dir, 5)
        shown = ", ".join(f"{d if d != '.' else '(top)'}: {c}"
                          for d, c in top_dirs)
        if len(by_dir) > 5:
            shown += f" (... and {len(by_dir) - 5} more)"
        lines.append(f"Folders: {shown}")
    if data["unreadable"]:
        lines.append(f"Skipped {len(data['unreadable'])} unreadable "
                     f"file(s)")
    lines.append("Generated by nightcore.py @ "
                 + datetime.now().strftime("%Y-%m-%d %H:%M"))
    return lines


def run_stats(stats_dir, max_depth=1):
    """Print folder statistics; returns a process-style exit code."""
    missing = missing_tools("ffprobe")
    if missing:
        print("Error: required tools not found on PATH: ffprobe.\n"
              "Install FFmpeg (provides ffmpeg + ffprobe) and re-run.",
              file=sys.stderr)
        return 127
    d = Path(stats_dir)
    if not d.is_dir():
        print(f"Error: not a directory: {d}", file=sys.stderr)
        return 1
    try:
        data = collect_stats(d, progress=True, max_depth=max_depth)
    except OSError as e:
        print(f"Error: cannot read directory {d}: {e}", file=sys.stderr)
        return 1
    if not data["audio"] and not data["lrc_files"]:
        print(f"Error: no audio files in {d}", file=sys.stderr)
        return 1
    if not data["audio"]:
        print(f"Error: no audio files in {d}", file=sys.stderr)
        return 1
    for name in data["unreadable"]:
        print(f"Warning: skipping unreadable file: {name}",
              file=sys.stderr)
    for line in render_stats(data):
        print(line)
    return 0
