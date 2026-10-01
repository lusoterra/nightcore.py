"""Reading and writing metadata tags and titles."""

from pathlib import Path
import re

from .audio import probe_audio
from .constants import _BITRATE_TAG, _DEPTH_TAG, _RATE_TOKEN, _SHIFT_TOKEN, _TAG_ALIASES

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
    """Present format tags as {lowercased-name: stripped value}."""
    wanted = {str(n).lower() for n in names}
    tags = probe_audio(input_path)["format_tags"] or {}
    return {
        str(k).lower(): str(v).strip()
        for k, v in tags.items()
        if str(k).lower() in wanted and str(v).strip()
    }


def read_format_tag(input_path, tag):
    """Single format tag value (stripped), or None if missing/unreadable."""
    for k, v in (probe_audio(input_path)["format_tags"] or {}).items():
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


def strip_script_tags(name):
    """Remove [...] groups this tool adds; return the cleaned name.

    Removes shift tags ([1.2x], [3.15st], combos like [1.2x 2.16st]),
    bare rate tags ([88.2kHz]), recorded rubberband extras ([rb ...])
    and quality tags ([16bit 44.1kHz], [mp3 CBR 320k ...]). Anything
    else stays verbatim, e.g. [Greatest Hits].
    """
    def _strip(m):
        toks = m.group(1).split()
        if not toks:
            return m.group(0)
        if toks[0] == "rb":
            return ""
        if all(_SHIFT_TOKEN.match(t) or _RATE_TOKEN.match(t) for t in toks):
            return ""
        if _BITRATE_TAG.match(m.group(0)) or _DEPTH_TAG.match(m.group(0)):
            return ""
        return m.group(0)

    return " ".join(re.sub(r"\[([^\[\]]*)\]", _strip, name).split())


def simple_title(title, suffix):
    """Clean a Title tag of script-added [...] groups; append suffix.

    Filenames are untouched (they keep their tags); only the embedded
    Title is cleaned, and foreign brackets like [Greatest Hits]
    survive. A non-empty suffix is separated by one space unless it
    already starts with whitespace.
    """
    cleaned = strip_script_tags(title)
    if suffix:
        if not suffix[0].isspace():
            suffix = " " + suffix
        cleaned = (cleaned + suffix).strip()
    return cleaned


def apply_template_tags(tmpl, input_path, original_title, artist,
                          keep_missing=False):
    """Apply a parsed tag template; returns (title_base, ff_extra,
    stub_encoder, artist).

    title_base/""/None rules, keep-source (bare value) lookups, muxer
    aliases and the effective-artist ride-along live here so conversions
    and renametag mode share them exactly. Never raises for missing tags.
    keep_missing (--no-delete-tags): keys absent from the template keep
    their source values instead of being stripped (title_base falls back
    to the source title, artist to the source artist).
    """
    ff_extra = {}
    title_base = original_title
    stub_encoder = None
    entry = tmpl.get("title")
    if entry is None:
        title_base = original_title if keep_missing else ""
    elif entry[1] is not None:
        title_base = entry[1]
    entry = tmpl.get("artist")
    if entry is None:
        artist = artist if keep_missing else None
    elif entry[1] is not None:
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
    return title_base, ff_extra, stub_encoder, artist, original_title


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


def compute_new_title(args, tmpl, title_base, original_title, suffix):
    """Final Title tag: --simple-title wins, then --autotag suffix."""
    if args.simple_title is not None:
        return simple_title(original_title + suffix, args.simple_title)
    if args.autotag:
        new_title = original_title + suffix
        if tmpl is not None and not title_base:
            new_title = new_title.lstrip()
        return new_title
    return original_title


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

    Keys keep their original case; empty values are dropped. Returns
    ({}, {}) when unreadable. Never raises.
    """
    probe = probe_audio(input_path)

    def _clean(tags):
        return {str(k): str(v) for k, v in (tags or {}).items()
                if str(v) != ""}

    return (_clean(probe["format_tags"]),
            _clean(probe["audio"].get("tags")))


