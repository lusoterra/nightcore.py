"""Reading and writing metadata tags and titles."""

from pathlib import Path
import math
import re

from . import __version__
from .audio import probe_audio
from .constants import _BITRATE_TAG, _DEPTH_TAG, _RATE_TOKEN, _SHIFT_TOKEN, _TAG_ALIASES, PROJECT_URL, STAMP_TAG
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


def tool_stamp(settings=None):
    """Provenance string for stamped outputs (default: always on).

    Written to the STAMP_TAG (DESCRIPTION) tag on every output; an
    explicit per-run mark, so it overrides any template description.
    When settings is given (encode parameters), it rides on a second
    line so a renamed file still carries enough info to reverse the
    job later with --denightcore.
    """
    base = f"Made with nightcore {__version__} ({PROJECT_URL})"
    if settings:
        return f"{base}\n{settings}"
    return base


def build_stamp_settings(*, speed=1.0, semitones=0.0, pitch_mode=False,
                         target_rate=None, out_format="flac",
                         lossy_opts=(), source_rate=None, source_bits=None,
                         source_codec=None, denightcore=None):
    """One-line settings payload for the stamp's second line.

    Carries everything needed to reverse the job: the effective speed
    factor and semitone shift applied. Resample target, output format
    and source fingerprint ride along for context. Space-separated
    key=value pairs (no spaces in values) so denightcore can parse it
    back even when the filename tags are gone.
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
    if denightcore:
        parts.append(f"denightcore={denightcore}")
    return "Settings: " + " ".join(parts)


def backfill_stamp_settings(stem, *, sample_rate=None, bits=None,
                            codec=None, out_format="flac"):
    """Rebuild a stamp Settings line from filename shift tags, or None.

    Reads [Nx]/[Nst]/[Nightcore Nx] groups out of a stem left behind by
    an older run (or a rename that kept them) so renametag mode can write
    a proper stamp -- and a later -D can undo from it. Restored rate and
    depth describe the pre-shift audio, matching what a forward job would
    have recorded. Returns None when the stem carries no shift tags.
    """
    from .audio import speed_from_semitones
    from .naming import split_shift_tags
    _, speed, st, found, _ = split_shift_tags(stem)
    if not found or not speed:
        return None
    restored = None
    if sample_rate is not None:
        try:
            restored = round(
                sample_rate / speed / speed_from_semitones(st))
        except (ValueError, ZeroDivisionError, OverflowError):
            restored = None
    return build_stamp_settings(
        speed=speed, semitones=st, pitch_mode=(st != 0.0),
        target_rate=None, out_format=out_format, lossy_opts=(),
        source_rate=restored, source_bits=bits, source_codec=codec)


def parse_stamp_shifts(stamp_text):
    """(speed, semitones) recovered from a stamp settings line, or None.

    Reads the machine-readable second line written by
    build_stamp_settings (speed=<float>x, semitones=<float>st). Used by
    --denightcore as a fallback when the filename carries no shift
    tags. Returns None when nothing parseable is found.
    """
    if not stamp_text:
        return None
    if "denightcore=" in stamp_text:
        # Restoration records (written by -D outputs) must never feed
        # another undo: a denightcored file has nothing left to invert.
        return None
    m_speed = re.search(r"speed=(-?\d+(?:\.\d+)?)x", stamp_text)
    m_st = re.search(r"semitones=(-?\d+(?:\.\d+)?)st", stamp_text)
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


def render_autorename(args, artist, title, fallback_stem, *, diff="",
                      quality="", resample="", denightcore=""):
    """Filename stem for --autorename (bare or TEMPLATE), or None if off.

    Bare --autorename (empty string const) keeps the legacy behavior:
    "Artist - Title" (artist prefix omitted without an artist tag).
    A TEMPLATE value renders through {artist}/{title}/{diff}/{quality}/
    {resample}/{denightcore} with user-supplied brackets. Returns None
    when --autorename was not passed (caller keeps the input stem).
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


def round_diff_bare(diff, places):
    """Round every number in a bare diff string to `places` decimals.

    "2.166866948st" with 2 -> "2.17st"; combos round each number
    ("1.2x 2.166st" -> "1.2x 2.17st"). Trailing zeros are stripped, so
    exact values stay clean ("1.2x", not "1.20x"). Display-only: the
    filename and stamp always keep full precision.
    """
    def _round(m):
        try:
            value = float(m.group(0))
        except ValueError:
            return m.group(0)
        if places == 0:
            return str(int(round(value)))
        return f"{round(value, places):.{places}f}".rstrip("0").rstrip(".")
    return re.sub(r"-?\d+(?:\.\d+)?", _round, diff or "")


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
    src_slots = title_context(
        title=original_title, artist=artist or "",
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
    for low, (origkey, val) in tmpl.items():
        if low in ("title", "artist"):
            continue
        key = _TAG_ALIASES.get(low, origkey)
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


