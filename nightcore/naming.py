"""Pure filename/tag-string logic and output-path helpers."""

from pathlib import Path
import re

from .constants import LOSSY_CODECS, _BITRATE_TAG, _DEPTH_TAG, _QUALITY_CODECS, _RATE_TOKEN, _SHIFT_TOKEN

def strip_denightcore_tags(name):
    """Remove our own [denightcore ...] verdict groups; return cleaned name.

    A verdict describes a past job, so it goes stale the moment new audio
    work happens: without this, nightcore -> denightcore -> nightcore
    cycles would accumulate "[denightcored BP] [1.3x] [denightcored BP] ..."
    in titles and filenames. Anything else stays verbatim.
    """
    return " ".join(
        re.sub(r"\[denightcore[^\[\]]*\]", "", name,
               flags=re.IGNORECASE).split())


def clean_source_title(name):
    """Drop our script groups from an inherited Title tag.

    Removes shift/display tags ([1.2x], [Nightcore 1.2x]), quality and
    rate tags ([16bit 44.1kHz], [88.2kHz]) and past verdicts
    ([denightcored BP]) so re-encodes don't accumulate
    "[Nightcore 1.2x] [1.3x] ..." in titles. Foreign brackets
    ([Greatest Hits], [Nightcore's Mix]) survive; explicit template
    titles bypass this entirely (used verbatim).
    """
    return strip_quality_rate_tags(
        split_shift_tags(strip_denightcore_tags(name))[0])


def strip_empty_display_tags(name):
    """Remove vacant [Nightcore] display groups (no shift inside).

    Leftover of rendering "[Nightcore {diff}]" with an empty diff: the
    group carries no information, so unlike meaningful tags it is safe
    to drop even where titles are otherwise kept verbatim (renametag).
    """
    return " ".join(
        re.sub(r"\[nightcore\s*\]", "", name,
               flags=re.IGNORECASE).split())


_QUALITY_GROUP = re.compile(
    r"^\[([\w-]+)(?:\s+(VBR|CBR))?\s+(\d+)k(?:\s+(.*?))?\]$",
    re.IGNORECASE)
_DEPTH_GROUP = re.compile(r"^\[(\d+)bit\s+(\d+(?:\.\d+)?)kHz\]$",
                          re.IGNORECASE)
_BARE_RATE_GROUP = re.compile(r"^(\d+(?:\.\d+)?)kHz$", re.IGNORECASE)


def recover_filename_quality(stem):
    """Recover our filename quality/resample groups plus parsed origin.

    Reads [codec [mode] bitratek [encoder]] and [Nbit N.NkHz] groups and
    a trailing bare [NkHz] resample group out of a stem, verbatim. Shift
    and foreign brackets are ignored. Returns {"suffix", "codec", "mode",
    "bitrate_kbps", "encoder", "depth", "rate_hz"} -- codec in
    probe-style underscore form so verdict checks match, Nones when
    absent. The filename was written from true probe data at encode
    time, so for origin questions it outranks the container probe (a
    FLAC wrapping an MP3 probes as flac).
    """
    quality = resample = ""
    codec = mode = bitrate_kbps = encoder = depth = rate_hz = None
    resample_hz = None
    for m in re.finditer(r"\[([^\[\]]*)\]", stem):
        full = m.group(0)
        inner = m.group(1).strip()
        qm = _QUALITY_GROUP.match(full)
        if (qm and qm.group(1).lower().replace("_", "-")
                in _QUALITY_CODECS and not quality):
            quality = full
            codec = qm.group(1).lower().replace("-", "_")
            mode = (qm.group(2) or "").upper() or None
            bitrate_kbps = int(qm.group(3))
            encoder = (qm.group(4) or "").strip() or None
            continue
        dm = _DEPTH_GROUP.match(full)
        if dm and not quality:
            quality = full
            depth = int(dm.group(1))
            rate_hz = round(float(dm.group(2)) * 1000)
            continue
        rm = _BARE_RATE_GROUP.match(inner)
        if rm:
            resample = full
            resample_hz = round(float(rm.group(1)) * 1000)
    suffix = (f" {quality}" if quality else "") + \
        (f" {resample}" if resample else "")
    return {"suffix": suffix, "codec": codec, "mode": mode,
            "bitrate_kbps": bitrate_kbps,
            "encoder": encoder, "depth": depth, "rate_hz": rate_hz,
            "resample_hz": resample_hz}


def split_shift_tags(name):
    """Strip shift/rate/rb [...] groups; return (cleaned, speed, st, found, rb).

    Removes bracket groups made only of shift tokens ([1.2x], [3.15st],
    combined [1.2x 2.16st]), display groups this tool writes into titles
    ([Nightcore 1.2x], i.e. a leading "Nightcore" marker before shift
    tokens), bare rate tags ([88.2kHz]), and recorded
    rubberband extras ([rb --formant]). Quality tags ([mp3 CBR 320k LAME],
    [16bit 44.1kHz]) and anything else stay verbatim. speed = product of
    [Nx], st = sum of [Nst]; found tells whether any shift token was seen
    at all; rb is the recorded extras string of the LAST [rb ...] group
    (or None).
    """
    speed, st, found = 1.0, 0.0, False
    rb_groups = []

    def _strip(m):
        nonlocal speed, st, found
        toks = m.group(1).split()
        if toks and toks[0].lower() == "nightcore":
            # Display flavor of a shift tag ("Song [Nightcore 1.2x]"):
            # same stripping/parsing as the bare form. A bare
            # "[Nightcore]" carries no shift at all (leftover of an
            # empty render), so it goes too. Anything else after the
            # marker (e.g. "[Nightcore's Mix]") stays verbatim.
            toks = toks[1:]
            if not toks:
                return ""
        if toks and toks[0] == "rb":
            rb_groups.append(" ".join(toks[1:]))
            return ""
        if toks and all(
            _SHIFT_TOKEN.match(t) or _RATE_TOKEN.match(t) for t in toks
        ):
            for t in toks:
                ms = _SHIFT_TOKEN.match(t)
                if ms is None:
                    continue
                found = True
                if ms.group(2) == "x":
                    speed *= float(ms.group(1))
                else:
                    st += float(ms.group(1))
            return ""
        return m.group(0)

    cleaned = " ".join(re.sub(r"\[([^\[\]]*)\]", _strip, name).split())
    return cleaned, speed, st, found, (rb_groups[-1] if rb_groups else None)


def strip_quality_tags(name):
    """Remove quality [...] groups only ([16bit 44.1kHz],
    [mp3 CBR 320k LAME3.100]). Bare rate tags stay (see
    strip_rate_tags); anything else stays verbatim.
    """
    def _strip(m):
        if _BITRATE_TAG.match(m.group(0)) or _DEPTH_TAG.match(m.group(0)):
            return ""
        return m.group(0)

    return " ".join(re.sub(r"\[([^\[\]]*)\]", _strip, name).split())


def strip_rate_tags(name):
    """Remove bare rate [...] groups only ([88.2kHz], [96kHz]).
    Anything else stays verbatim.
    """
    def _strip(m):
        toks = m.group(1).split()
        if toks and all(_RATE_TOKEN.match(t) for t in toks):
            return ""
        return m.group(0)

    return " ".join(re.sub(r"\[([^\[\]]*)\]", _strip, name).split())


def strip_quality_rate_tags(name):
    """Remove quality/rate [...] groups; return the cleaned name.

    Removes bare rate tags ([88.2kHz], same rule as split_shift_tags)
    and quality tags in exactly the shapes this tool writes ([16bit
    44.1kHz], [mp3 CBR 320k LAME3.100]). Anything else stays verbatim,
    so real title brackets (e.g. [Greatest Hits], [Opus 1]) survive.
    Used for lossy outputs, which carry no source-quality or rate tags.
    """
    return strip_rate_tags(strip_quality_tags(name))


def lossy_suffix_for(codec, bit_rate, mode=None, encoder=None):
    """Build ' [codec [mode] bitratek [encoder]]' tag for lossy sources.

    Returns '' when codec is not a known lossy codec. Mode ('VBR'/'CBR')
    and encoder are included only when detected, e.g. [mp3 CBR 320k LAME3.100].
    """
    if codec not in LOSSY_CODECS:
        return ""
    # Normalize amr variants for display: amr_nb -> amr, etc.
    display_codec = codec.replace("_", "-")
    parts = [display_codec]
    if mode:
        parts.append(mode)
    if bit_rate is not None:
        kbps = round(bit_rate / 1000)
        if kbps > 0:
            parts.append(f"{kbps}k")
    if encoder:
        parts.append(encoder)
    return " [" + " ".join(parts) + "]"


def batch_op_dir(args, op, fmt=None):
    """Directory for a batch operation folder, honoring --output-folder.

    op is "encodes", "denightcore" or "renametag";
    fmt appends the codec subfolder (encodes only: std-samplerate lives
    flat inside it, denightcore/renametag take none).
    No --output-folder: ./<op>[/<fmt>]. Existing --output-folder dir X:
    X/<op>[/<fmt>]. Missing X: X[/<fmt>] itself (the leaf takes the op
    folder's place). Callers mkdir (except dry-run).
    """
    sub = f"{op}/{fmt}" if fmt else op
    if args.output_folder is None:
        return Path.cwd() / sub
    x = Path(args.output_folder)
    if x.is_dir():
        return x / sub
    return x / (fmt or "")


def single_parent(args, input_path):
    """Output directory for single-file runs: --output-folder wins,
    else alongside the input. Created unless dry-run."""
    if args.output_folder is None:
        return input_path.parent
    parent = Path(args.output_folder)
    if not args.dry_run:
        parent.mkdir(parents=True, exist_ok=True)
    return parent


def claim_output_path(ctx, output_path):
    """Batch-collision guard: first claim keeps the name, later ones get
    " (2)", " (3)", ... Full paths are tracked, so distinct directories
    never collide."""
    claimed = ctx.setdefault("claimed_outputs", set())
    candidate = output_path
    n = 2
    while candidate in claimed:
        candidate = output_path.with_name(
            f"{output_path.stem} ({n}){output_path.suffix}")
        n += 1
    claimed.add(candidate)
    return candidate
