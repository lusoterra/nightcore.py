"""Reading and writing metadata tags and titles."""

from pathlib import Path
import math
import re

from . import __version__
from .audio import probe_audio
from .constants import _BITRATE_TAG, _DEPTH_TAG, _RATE_TOKEN, _SHIFT_TOKEN, _TAG_ALIASES, PIPELINE_MAGIC, PIPELINE_TAG, PROJECT_URL, STAMP_TAG
from .native_tags import native_format_tags

def parse_tag_template(path):
    """Parse a tag template file; return ordered {lowerkey: (origkey, value|None)}.

    Line format is key="value" (values may span lines, newlines preserved),
    bare key=, or bare key. A None value means "keep the source tag".
    Blank lines and # comments (outside values) are skipped. Values cannot
    contain a line ending in a double quote. Raises OSError.
    """
    entries = {}
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        i += 1
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            if s:
                entries[s.lower()] = (s, None)
            continue
        key, _, rest = s.partition("=")
        key = key.strip()
        if not key:
            continue
        rest = rest.strip()
        if rest.startswith('"'):
            rest = rest[1:]
            if rest.endswith('"'):
                value = rest[:-1]
                entries[key.lower()] = (key, value if value != "" else None)
                continue
            buf = [rest]
            while i < n:
                ln = lines[i]
                i += 1
                stripped = ln.rstrip()
                if stripped.endswith('"'):
                    buf.append(stripped[:-1])
                    break
                buf.append(ln)
            else:
                raise ValueError(f"unterminated quoted value for tag '{key}'")
            entries[key.lower()] = (key, "\n".join(buf))
        else:
            entries[key.lower()] = (key, rest if rest != "" else None)
    return entries


def clean_encoder(enc, codec):
    """Normalize an encoder tag for display, or None if uninformative.

    Drops muxer version tokens (Lavf/Lavc); omits the encoder when it
    merely restates the codec with no version info (e.g. bare 'libopus'
    for opus) while keeping versioned ones ('libopus 1.5.2').
    """
    if not enc:
        return None
    kept = " ".join(
        t for t in str(enc).split() if not re.match(r"(?i)^lav[cf]\d", t)
    ).strip()
    if not kept:
        return None
    if codec and codec.lower() in kept.lower() and not re.search(r"\d", kept):
        return None
    return kept[:32]


def read_tags(input_path, *names):
    """Present format tags as {lowercased-name: stripped value}.

    Vorbis-family formats (flac/opus/ogg) read via their native tools
    (ffprobe aliases DESCRIPTION to "comment" there and cannot parse
    opus at all); everything else uses the ffprobe result.
    """
    wanted = {str(n).lower() for n in names}
    own = native_format_tags(input_path)
    tags = own if own is not None else probe_audio(input_path)["format_tags"] or {}
    return {
        str(k).lower(): str(v).strip()
        for k, v in tags.items()
        if str(k).lower() in wanted and str(v).strip()
    }


def tool_stamp():
    """Provenance string for stamped outputs (--no-stamp disables).

    Written to the STAMP_TAG (DESCRIPTION) tag on every output; an
    explicit per-run mark, so it overrides any template description.
    Branding only -- the machine-readable pipeline lives in PIPELINE_TAG
    (build_pipeline) so the two can be controlled independently.
    "nightcore.py" names the project; bare "nightcore" is the binary.
    """
    return f"Made with nightcore.py {__version__} ({PROJECT_URL})"


def build_pipeline(*, speed=1.0, semitones=0.0, pitch_mode=False,
                   target_rate=None, out_format="flac", lossy_opts=(),
                   source_rate=None, source_bits=None, source_codec=None,
                   source_mode=None, source_bitrate=None,
                   source_encoder=None, denightcore=None, tools=(),
                   stages=None):
    """Machine-readable encode record for PIPELINE_TAG (opusenc-style).

    Carries everything needed to remake or reverse the job: the
    effective speed factor and semitone shift applied, resample target,
    output format, source fingerprint, the lossy encoder flags when any,
    and the versions of the tools that ran (ffmpeg first, in pipeline
    order), each with the stage arguments it ran (audio-filter chains,
    sox rate targets, rubberband flags, dither). Space-separated key=value
    grammar shared with the legacy DESCRIPTION Settings line, so
    denightcore parses either tag, filenames or no.
    """
    parts = [f"speed={speed!r}x"]
    if pitch_mode or (semitones is not None and semitones != 0.0):
        parts.append(f"semitones={semitones!r}st")
    if target_rate is not None:
        parts.append(f"resample={target_rate}Hz")
    parts.append(f"format={out_format}")
    if lossy_opts:
        parts.append(f"lossy-opts={' '.join(lossy_opts)}")
    if source_rate is not None:
        parts.append(f"source-rate={source_rate}Hz")
    if source_bits is not None:
        parts.append(f"source-depth={source_bits}bit")
    if source_codec:
        parts.append(f"source-codec={source_codec}")
    if source_mode:
        parts.append(f"source-mode={source_mode}")
    if source_bitrate:
        parts.append(f"source-bitrate={source_bitrate}")
    if source_encoder:
        parts.append(f"source-encoder={source_encoder}")
    if denightcore:
        parts.append(f"denightcore={denightcore}")
    if tools:
        parts.append("tools=" + ",".join(
            f"{name}={version}" + (
                f"[{','.join((stages or {}).get(name, []))}]"
                if stages and stages.get(name) else "")
            for name, version in tools))
    return f"{PIPELINE_MAGIC} " + " ".join(parts)


def ensure_pipeline_magic(text):
    """Prepend the pipeline magic marker unless already present.

    Used when -N normalizes old records (preserved or migrated): the
    content is untouched, only the marker is ensured, so strict readers
    accept it afterwards. Empty input stays empty.
    """
    stripped = (text or "").strip()
    if not stripped or stripped.startswith(PIPELINE_MAGIC):
        return stripped
    return f"{PIPELINE_MAGIC} {stripped}"


def display_suffix(diff):
    """Filename diff suffix in display form (" [Nightcore 1.2x]").

    `diff` is the bare rounded label (format_diff_label output); empty
    diff renders nothing. Single source for the shape so fresh encodes,
    -D-adjacent paths and renametag rebuilds all print it identically.
    """
    return f" [Nightcore {diff}]" if (diff or "").strip() else ""


def format_diff_label(speed=1.0, st=0.0):
    """Bare diff label from a (speed, semitones) pair: "1.2x", "2.17st",
    "1.2x 2.17st" or "" when there is no shift. Display precision (2
    decimals); audio math and pipeline records use full precision.
    """
    parts = []
    if speed != 1.0:
        parts.append(f"{short_num(speed)}x")
    if st != 0.0:
        parts.append(f"{short_num(st)}st")
    return " ".join(parts)


def backfill_stamp_settings(stem, *, sample_rate=None, bits=None,
                            codec=None, out_format="flac", origin=None):
    """Rebuild a pipeline record from filename tags, or None.

    Reads [Nx]/[Nst]/[Nightcore Nx] groups out of a stem left behind by
    an older run (or a rename that kept them) so renametag mode can write
    a proper pipeline -- and a later -D can undo from it. The origin
    fingerprint prefers the filename quality groups (see
    recover_filename_quality): the container probe cannot see through a
    lossless wrapper around a lossy source. A trailing bare [rate] group
    is recorded as the resample target. Returns None when the stem
    carries no shift tags.
    """
    from .audio import speed_from_semitones
    from .naming import split_shift_tags
    _, speed, st, found, _ = split_shift_tags(stem)
    if not found or not speed:
        return None
    origin = origin or {}
    bitrate = (f"{origin['bitrate_kbps']}k"
               if origin.get("bitrate_kbps") else None)
    if origin.get("codec"):
        codec = origin["codec"]
    elif origin.get("depth") is not None:
        bits = origin["depth"]
    if origin.get("rate_hz") is not None:
        restored = origin["rate_hz"]
    elif sample_rate is not None:
        try:
            restored = round(
                sample_rate / speed / speed_from_semitones(st))
        except (ValueError, ZeroDivisionError, OverflowError):
            restored = None
    else:
        restored = None
    # Tool record, inferred from structure: our pipeline always pairs
    # pitch with sox+rubberband and resampling with sox, so a tag-only
    # recovery still names the stages that must have run.
    from .backends import tool_version
    _tools = ["ffmpeg"]
    if st != 0.0 or origin.get("resample_hz") is not None:
        _tools.append("sox")
    if st != 0.0:
        _tools.append("rubberband")
    return build_pipeline(
        speed=speed, semitones=st, pitch_mode=(st != 0.0),
        target_rate=origin.get("resample_hz"), out_format=out_format,
        lossy_opts=(), source_rate=restored, source_bits=bits,
        source_codec=codec, source_mode=origin.get("mode"),
        source_bitrate=bitrate,
        source_encoder=origin.get("encoder"),
        tools=[(t, tool_version(t)) for t in _tools])


def augment_pipeline_tools(pipe_text):
    """Add provably-missing stage tools to a pipeline record.

    A record with semitones= must have run rubberband (and sox for its
    rate steps); one with resample= must have run sox. Old records that
    predate tool recording (or filename-only backfills from before the
    inference) gain exactly those entries, with versions probed live;
    complete records pass through untouched. Stage tools only: lossy CLI
    encoders and metaflac can't be proven from parameters, so they are
    never added here.
    """
    from .backends import tool_version
    if not pipe_text or not pipe_text.strip():
        return pipe_text
    st = 0.0
    m_st = re.search(r"semitones=(-?\d+(?:\.\d+)?)st", pipe_text)
    if m_st:
        try:
            st = float(m_st.group(1))
        except ValueError:
            st = 0.0
    want = ["ffmpeg"]
    if st != 0.0 or "resample=" in pipe_text:
        want.append("sox")
    if st != 0.0:
        want.append("rubberband")
    present = set(re.findall(r"(?:^|[\s,=])(ffmpeg|sox|rubberband)=",
                             pipe_text))
    missing = [t for t in want if t not in present]
    if not missing:
        return pipe_text
    added = ",".join(f"{t}={tool_version(t)}" for t in missing)
    if "tools=" not in pipe_text:
        return pipe_text.rstrip() + f" tools={added}"
    return re.sub(r"tools=([^\s]+)",
                  lambda m: f"tools={m.group(1)},{added}", pipe_text,
                  count=1)


def pipeline_has(text, key):
    """True when the pipeline record sets key as a standalone entry.

    Delimited matching: stage arguments like dither=aresample=... must
    not trip over key names they merely contain.
    """
    return re.search(r"(?:^|\s)" + re.escape(key) + "=",
                     text or "") is not None


def pipeline_origin(text):
    """{codec, mode, bitrate, encoder, depth, rate} parsed from a pipeline
    record (raw strings, e.g. bitrate "320k", depth "16bit"). All None
    when absent. Used to carry a known lossy origin through -D instead
    of re-probing the wrapper container.
    """
    def _grab(key):
        match = re.search(r"(?:^|\s)" + key + r"=([^\s]+)", text or "")
        return match.group(1) if match else None

    def _int(value, suffix):
        if value and value.endswith(suffix):
            try:
                return int(value[:-len(suffix)])
            except ValueError:
                return None
        return None

    bitrate = _grab("source-bitrate")
    depth = _grab("source-depth")
    rate = _grab("source-rate")
    return {"codec": _grab("source-codec"),
            "mode": _grab("source-mode"),
            "bitrate": bitrate,
            "encoder": _grab("source-encoder"),
            "depth": _int(depth, "bit"),
            "rate": _int(rate, "Hz")}


def read_shift_source(input_path):
    """((speed, semitones) or None, raw tag text) for --denightcore.

    Reads the machine-readable PIPELINE_TAG, falling back to a legacy
    DESCRIPTION Settings line (pre-split files). Filenames are never
    consulted. The raw text rides along for verdict checks (resample=,
    source-codec=). Restoration records never parse: a denightcored
    file has nothing left to invert.
    """
    for tag in (PIPELINE_TAG, STAMP_TAG):
        text = read_format_tag(input_path, tag)
        if not text:
            continue
        parsed = parse_stamp_shifts(text)
        if parsed is not None:
            return parsed, text
    return None, ""


def parse_stamp_shifts(stamp_text):
    """(speed, semitones) recovered from tag pipeline text, or None.

    Reads the machine-readable grammar written by build_pipeline
    (speed=<float>x, semitones=<float>st). The pipeline magic marker is
    required: records without it (foreign tags, pre-magic stamps) are
    refused so nothing ever acts on data it did not write. Returns None
    when nothing parseable is found.
    """
    if not stamp_text:
        return None
    if not stamp_text.strip().startswith(PIPELINE_MAGIC):
        return None
    if pipeline_has(stamp_text, "denightcore"):
        # Restoration records (written by -D outputs) must never feed
        # another undo: a denightcored file has nothing left to invert.
        return None
    m_speed = re.search(r"(?:^|\s)speed=(-?\d+(?:\.\d+)?)x", stamp_text)
    m_st = re.search(r"(?:^|\s)semitones=(-?\d+(?:\.\d+)?)st", stamp_text)
    if m_speed is None and m_st is None:
        return None
    try:
        speed = float(m_speed.group(1)) if m_speed else 1.0
        st = float(m_st.group(1)) if m_st else 0.0
    except ValueError:
        return None
    if not all(map(math.isfinite, (speed, st))) or speed <= 0:
        return None
    return speed, st


def read_format_tag(input_path, tag):
    """Single format tag value (stripped), or None if missing/unreadable.

    Same native-first sourcing as read_tags.
    """
    own = native_format_tags(input_path)
    tags = own if own is not None else probe_audio(input_path)["format_tags"] or {}
    for k, v in tags.items():
        if str(k).lower() == str(tag).lower():
            value = str(v).strip()
            return value if value else None
    return None


def get_title(input_path, fallback):
    return read_format_tag(input_path, "title") or fallback


def get_artist(input_path):
    """The 'artist' tag (not albumartist), or None."""
    return read_format_tag(input_path, "artist")


def sanitize_filename(name):
    """Make a string safe as a filename on Linux and Windows."""
    cleaned = "".join(
        "_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name
    )
    return cleaned.strip().rstrip(" .") or "_"


def render_name_template(tmpl_str, mapping):
    """Render a {title}/{artist}/{diff}/{quality}/{resample}/{denightcore} template.

    Placeholders expand bare (no brackets): the template supplies its
    own brackets, e.g. "{title} [{diff}]" or "{title} [Nightcore {diff}]".
    {diff(N)} rounds the shift for display ("{diff(2)}" on "2.1668st"
    gives "2.17st"; only diff supports this -- rounding other slots
    would corrupt values like encoder versions). Unknown {keys} are left
    verbatim so typos stay visible. Empty values collapse: vacant "[]"
    pairs are dropped and whitespace is squeezed, so "[{diff}]" with an
    empty diff vanishes instead of leaving "[]".
    """
    out = str(tmpl_str)
    mapping = mapping or {}

    def _drop_vacant(m):
        # Drop a bracket group whose placeholders ALL render empty:
        # "[Nightcore {diff}]" with no diff carries no information, so
        # it goes instead of leaving "[Nightcore ]" behind. Groups with
        # no placeholders ([Greatest Hits]) always survive, as do groups
        # with at least one live slot; unknown {keys} count as live so
        # typos stay visible.
        inner = m.group(1)
        keys = re.findall(
            r"\{(title|artist|diff|quality|resample|denightcore)"
            r"(?:\(\d+\))?\}", inner)
        if keys and all(not (mapping.get(k) or "") for k in keys):
            return ""
        return m.group(0)

    out = re.sub(r"\[([^\[\]]*)\]", _drop_vacant, out)

    def _diff_mod(m):
        return round_diff_bare(mapping.get("diff") or "", int(m.group(1)))

    out = _DIFF_MOD_PATTERN.sub(_diff_mod, out)
    for key in ("title", "artist", "diff", "quality", "resample",
                "denightcore"):
        out = out.replace("{" + key + "}", str(mapping.get(key) or ""))
    # Drop brackets left empty by vacant placeholders: "[  ]" -> "".
    for _ in range(4):
        new = re.sub(r"\[\s*\]", "", out)
        if new == out:
            break
        out = new
    return " ".join(out.split()).strip()


def title_context(*, title="", artist="", diff="", quality="",
                  resample="", denightcore=""):
    """Mapping for render_name_template (Title/Artist/filename)."""
    return {"title": title or "", "artist": artist or "", "diff": diff or "",
            "quality": quality or "", "resample": resample or "",
            "denightcore": denightcore or ""}


def compute_new_title(tmpl, title_base, original_title, suffix,
                      *, diff="", denightcore="", title_verbatim=False):
    """Final Title tag: default is source title + [Nightcore diff].

    The Title stays readable for players: only the rounded shift amount
    rides along ("Song [Nightcore 1.2x]"); full precision stays in the
    filename and stamp. A tag-template title= value is always used
    verbatim (rendered first if it used {slots}). The legacy suffix arg
    is kept for callers that still pass it: when diff is empty, it is
    parsed for a shift token fallback so old call paths keep working.
    """
    if title_verbatim:
        return original_title
    if diff:
        shown = round_diff_bare(diff, 2)
        if original_title:
            return f"{original_title} [Nightcore {shown}]"
        return f"[Nightcore {shown}]"
    if denightcore:
        # Denightcore undos carry the BP/NBP verdict instead of a shift.
        if original_title:
            return f"{original_title} [{denightcore}]"
        return f"[{denightcore}]"
    if suffix:
        # Fallback for legacy callers passing a prebuilt suffix without
        # split components (renametag passes ""): never triggers there.
        m = re.search(r"\[([^\[\]]*)\]", suffix)
        if m:
            inner = m.group(1).strip()
            if inner and original_title and inner not in original_title:
                return f"{original_title} [{inner}]"
    if tmpl is not None and not title_base:
        return original_title.lstrip()
    return original_title


def autorename_base(artist, rendered_title, fallback_stem, *,
                      drop_shift, drop_rate):
    """Bare --autorename base: "Artist - Title" (artist prefix omitted
    without an artist tag).

    Quality groups always go (filenames never carry quality anymore;
    the pipeline and explicit template {quality} do). Shift and bare
    rate groups go only where the filename suffix re-adds them:
    stripping a speed-1.0 transcode's inherited [1.1x] with nothing
    replacing it would delete true information. Verdicts and vacant
    display groups always go (never rewritten). Foreign brackets
    survive; an emptied title falls back to the stem.
    """
    from .naming import (split_shift_tags, strip_denightcore_tags,
                         strip_empty_display_tags, strip_quality_tags,
                         strip_rate_tags)
    title = strip_empty_display_tags(strip_denightcore_tags(rendered_title))
    if drop_shift:
        title = split_shift_tags(title)[0]
    title = strip_quality_tags(title)
    if drop_rate:
        title = strip_rate_tags(title)
    title = title.strip() or fallback_stem
    if artist:
        return f"{artist} - " + title
    return title


def render_autorename(args, artist, title, fallback_stem, *, diff="",
                      quality="", resample="", denightcore=""):
    """Filename stem for --autorename (bare/default TEMPLATE), or None.

    A bare --autorename flag renders the default template; an explicit
    TEMPLATE value renders through {artist}/{title}/{diff}/{quality}/
    {resample}/{denightcore} with user-supplied brackets (an empty
    --autorename= keeps the legacy "Artist - Title" spelling, artist
    prefix omitted without an artist tag). Callers pass
    the source title (never the tag template's, so the two can't double
    each other); {diff} is full precision ({diff(N)} rounds, as in tag
    templates). Returns None when --autorename was not passed (caller
    keeps the input stem).
    """
    tmpl = getattr(args, "autorename", None)
    if tmpl is None:
        return None
    if tmpl == "":
        if artist:
            return f"{artist} - " + title
        return title
    rendered = render_name_template(tmpl, title_context(
        title=title, artist=artist or "", diff=diff, quality=quality,
        resample=resample, denightcore=denightcore))
    if not artist:
        # No artist tag: don't leave a dangling " - " separator behind
        # (mirrors the legacy bare mode, which omits the artist prefix).
        rendered = re.sub(r"^\s*-\s+", "", rendered)
        rendered = re.sub(r"\s+-\s*$", "", rendered)
        rendered = " ".join(rendered.split()).strip()
    return rendered or fallback_stem


_SLOT_NAMES = ("title", "artist", "diff", "quality", "resample",
               "denightcore")

_SLOT_PATTERN = re.compile(
    r"\{(?:" + "|".join(_SLOT_NAMES) + r")\}")

_DIFF_MOD_PATTERN = re.compile(r"\{diff\((\d+)\)\}")


def template_uses_slots(value):
    """True when a tag-template value contains a {slot} placeholder."""
    return _SLOT_PATTERN.search(str(value)) is not None


def short_num(value, places=2):
    """Round a number for display: 2.166866948 -> "2.17", 2.0 -> "2".

    Trailing zeros are stripped so exact values stay clean. Display-only;
    audio math and the pipeline record always use full precision.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if places == 0:
        return str(int(round(number)))
    return f"{round(number, places):.{places}f}".rstrip("0").rstrip(".")


def round_diff_bare(diff, places):
    """Round every number in a bare diff string to `places` decimals.

    "2.166866948st" with 2 -> "2.17st"; combos round each number
    ("1.2x 2.166st" -> "1.2x 2.17st"). Display-only: audio math and the
    pipeline record always keep full precision.
    """
    return re.sub(r"-?\d+(?:\.\d+)?",
                  lambda m: short_num(m.group(0), places), diff or "")


def apply_template_tags(tmpl, input_path, original_title, artist,
                        keep_missing=False, slots=None):
    """Apply a parsed tag template; returns (title_base, ff_extra,
    stub_encoder, artist, original_title, title_verbatim).

    title_base/""/None rules, keep-source (bare value) lookups, muxer
    aliases and the effective-artist ride-along live here so conversions
    and renametag mode share them exactly. Never raises for missing tags.
    keep_missing (--no-delete-tags): keys absent from the template keep
    their source values instead of being stripped (title_base falls back
    to the source title, artist to the source artist).
    slots: per-job {diff}/{quality}/{resample}/{denightcore} mapping (or
    None to skip expansion). Only title=/artist= values expand slots --
    every other value stays literal, so prose like comments is safe.
    {title}/{artist} inside those values refer to the SOURCE tags.
    Any explicit title= value is used verbatim (title_verbatim): what you
    write is what every file gets, e.g. title="My Mix" titles everything
    "My Mix". Titles that fall back to the source (bare title=, title=""
    or no title= entry) still get the default [diff]/[denightcore]
    treatment afterwards.
    """
    ff_extra = {}
    title_base = original_title
    stub_encoder = None
    title_verbatim = False
    # The {title} slot always sees the source title cleaned of our script
    # groups (a template re-rendering "{title} [Nightcore {diff}]" must
    # not duplicate stale "[1.2x]" groups riding in from an old file).
    # Everything else (fallbacks, explicit literals) is untouched.
    from .naming import clean_source_title
    src_slots = title_context(
        title=clean_source_title(original_title), artist=artist or "",
        diff=(slots or {}).get("diff", ""),
        quality=(slots or {}).get("quality", ""),
        resample=(slots or {}).get("resample", ""),
        denightcore=(slots or {}).get("denightcore", ""))
    entry = tmpl.get("title")
    if entry is None:
        title_base = original_title if keep_missing else ""
    elif entry[1] is not None:
        if template_uses_slots(entry[1]):
            title_base = render_name_template(entry[1], src_slots)
        else:
            title_base = entry[1]
        title_verbatim = True
    entry = tmpl.get("artist")
    if entry is None:
        artist = artist if keep_missing else None
    elif entry[1] is not None:
        if template_uses_slots(entry[1]):
            artist = render_name_template(entry[1], src_slots) or None
        else:
            artist = entry[1]
    keep_src = [k for k, (_, v) in tmpl.items()
                if v is None and k not in ("title", "artist")]
    src_vals = read_tags(input_path, *keep_src) if keep_src else {}
    for low, (_, val) in tmpl.items():
        if low in ("title", "artist"):
            continue
        # Lowercase tag names at the template layer: Vorbis comments are
        # case-insensitive, MP3 frame mapping and movenc atoms resolve
        # lowercase keys to the proper frames/atoms (verified), and every
        # reader here already compares case-insensitively. The movenc
        # albumartist alias below is unaffected (already lowercase).
        key = _TAG_ALIASES.get(low, low)
        if val is not None:
            ff_extra[key] = val
        elif low in src_vals:
            ff_extra[key] = src_vals[low]
    # The effective artist rides along so every encoder path that
    # takes an explicit tag dict stays in sync.
    if artist is not None:
        ff_extra["artist"] = artist
    # A template `encoder` entry is honored on FLAC via metaflac (the
    # only writer that allows custom values); other formats warn later.
    stub_encoder = None
    for key, value in ff_extra.items():
        if key.lower() == "encoder":
            stub_encoder = value
            break
    original_title = title_base
    return (title_base, ff_extra, stub_encoder, artist, original_title,
            title_verbatim)


def fold_album_artist(tags):
    """Return mapping with any album_artist spelling folded to albumartist.

    ALBUMARTIST is the canonical Vorbis/ID3 form (what Picard writes and
    players expect); the movenc alias (albumartist -> album_artist, needed
    because movenc drops the bare spelling) must not leak into other
    containers. The album_artist value wins when both spellings somehow
    coexist. Case-insensitive; never raises.
    """
    out = dict(tags)
    hits = [k for k in out if str(k).lower() == "album_artist"]
    for key in hits:
        out["albumartist"] = out.pop(key)
    return out


# movenc stores the album artist under the exact "album_artist" key
# (aART atom); these alternate spellings must be rewritten to it for
# m4a/mp4 outputs or movenc drops them silently. Same direction the
# template layer's _TAG_ALIASES already applies, mirrored for the
# source-tag pass-through (which passes stored-case keys verbatim).
_MOVENC_ALBUM_ARTIST = "album_artist"
_MOVENC_ALBUM_ARTIST_FALLBACKS = {"albumartist", "album artist"}


def movenc_tag_key(key):
    """Stored-case tag key rewritten to movenc's spelling, or as-is.

    movenc (m4a/mp4) recognizes only the exact album_artist key for the
    aART atom and silently drops every other album-artist spelling
    (ALBUMARTIST from Vorbis comments, bare albumartist, "Album
    Artist"), so pass-through tags need this remap there; every other
    container keeps the source's spelling (mp3 stores a TXXX frame,
    FLAC/Vorbis store the comment verbatim), which is why the remap is
    mov-only. The match is case-insensitive on both sides (movenc
    resolves keys case-insensitively). Never raises.
    """
    if str(key).lower() in _MOVENC_ALBUM_ARTIST_FALLBACKS:
        return _MOVENC_ALBUM_ARTIST
    return key


def movenc_tag_map(tags):
    """Mapping with every key run through movenc_tag_key.

    Several album-artist spellings collapse onto album_artist: a key
    already spelled album_artist wins (same precedence as
    fold_album_artist, mirrored), else the first spelling seen wins.
    Never raises.
    """
    exact = any(str(k).lower() == _MOVENC_ALBUM_ARTIST for k in tags)
    out = {}
    for key, value in tags.items():
        low = str(key).lower()
        if exact and low in _MOVENC_ALBUM_ARTIST_FALLBACKS:
            continue
        new = movenc_tag_key(key)
        if new in out and new != key:
            continue  # first fallback spelling wins a collision
        out[new] = value
    return out


def flac_comment_wants(tmpl, ff_extra, input_path, out_ext, no_delete):
    """(want_comment, want_description) for the metaflac fix pass.

    Template entries win when present; with --no-delete-tags (or no
    template) the source values fill the gaps, otherwise ffmpeg's
    COMMENT-into-DESCRIPTION merge would silently degrade them.
    """
    want_c = want_d = None
    src_c = src_d = None
    if out_ext == ".flac" and (tmpl is None or no_delete):
        src = read_tags(input_path, "comment", "description")
        src_c, src_d = src.get("comment"), src.get("description")
    if tmpl is not None:
        low = {str(k).lower(): v for k, v in ff_extra.items()}
        want_c = low.get("comment", src_c)
        want_d = low.get("description", src_d)
    else:
        want_c, want_d = src_c, src_d
    return want_c, want_d


def describe_output_tags(tmpl, no_delete, ff_count, backend_kind, out_ext,
                         flag_cover, orig_cover):
    """Short (tag_mode, cover_mode) strings for dry-run previews."""
    if tmpl is not None:
        mode = (f"template ({ff_count} set, "
                f"{'keep' if no_delete else 'strip'} unlisted)")
    elif backend_kind == "opusenc":
        mode = "source (native)"
    else:
        mode = "source mapped"
    if flag_cover is not None:
        cover = f"replace ({flag_cover.name})"
    elif orig_cover is not None:
        cover = "original (keep)" if out_ext != ".opus" else "original (opusenc)"
    else:
        cover = "none"
    return mode, cover


def read_all_tags(input_path):
    """All format + first-audio-stream tags as (fmt_dict, stream_dict).

    Keys keep their original case; empty values are dropped. Format
    tags come from the native reader where one exists (see read_tags).
    Returns ({}, {}) when unreadable. Never raises.
    """
    probe = probe_audio(input_path)

    def _clean(tags):
        return {str(k): str(v) for k, v in (tags or {}).items()
                if str(v) != ""}

    own = native_format_tags(input_path)
    return (_clean(own if own is not None else probe["format_tags"]),
            _clean(probe["audio"].get("tags")))
