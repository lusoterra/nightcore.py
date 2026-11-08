"""Exact tag reads via format-native tools.

ffprobe's tag reporting fumbles parts of the vorbis-comment family: it
cannot parse opus tags at all, and it aliases a stored DESCRIPTION back
as "comment". The native readers below return stored fields verbatim
for the formats that need it; everything else keeps using ffprobe.

Readers are preferred, never required: a missing binary (or an
unreadable file) yields None and callers fall back to ffprobe, so the
tool keeps working with only ffmpeg installed. The PKGBUILD lists
these as hard dependencies.
"""

import re
import subprocess
from pathlib import Path

# Extension -> reader kind. Anything else reads via ffprobe.
_NATIVE = {
    ".flac": "metaflac",
    ".opus": "opusinfo",
    ".ogg": "vorbiscomment",
    ".oga": "vorbiscomment",
}

_native_cache = {}


def _run(cmd):
    """subprocess.run alias, so tests can stub tool output."""
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def native_format_tags(path):
    """{stored-case key: value} via the native tag reader, or None.

    None means no native reader covers this extension, its binary is
    missing, or the file is unreadable (callers fall back to ffprobe).
    An empty dict means the reader worked and found no fields. First
    occurrence wins on repeated fields. Results are cached by path for
    the run (same lifetime reasoning as probe_audio's cache). Never
    raises.
    """
    key = str(path)
    if key not in _native_cache:
        _native_cache[key] = _read_native(path)
    return _native_cache[key]


def _read_native(path):
    ext = Path(path).suffix.lower()
    kind = _NATIVE.get(ext)
    if kind is None:
        return None
    try:
        if kind == "metaflac":
            return _metaflac_tags(path)
        if kind == "opusinfo":
            return _opusinfo_tags(path)
        return _vorbiscomment_tags(path)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _metaflac_tags(path):
    """Parse `metaflac --list --block-type=VORBIS_COMMENT`.

    Field lines look like `    comment[3]: ARTIST=Code Red`; the vendor
    string is not a field and is ignored (ffprobe hides it too, so
    leaving it out keeps behavior identical where ffprobe works).
    Continuation lines (multiline values) rejoin their field.
    """
    result = _run(["metaflac", "--list",
                   "--block-type=VORBIS_COMMENT", str(path)])
    if result.returncode != 0:
        return None
    fields = {}
    current = None
    for line in result.stdout.splitlines():
        m = re.match(r"\s*comment\[\d+\]:\s*([^=]+)=(.*)$", line)
        if m:
            if m.group(1) not in fields:
                fields[m.group(1)] = m.group(2)
                current = m.group(1)
            else:
                current = None  # repeated field: keep first
        elif current is not None:
            fields[current] += "\n" + line
    return fields


def _opusinfo_tags(path):
    """Parse the `User comments section follows...` block of opusinfo.

    Fields are tab-indented `KEY=value` lines in stored case; the block
    ends at the first non-indented line. No marker means opusinfo could
    not parse the file (it exits 0 even on garbage), hence None.
    """
    result = _run(["opusinfo", str(path)])
    lines = result.stdout.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines)
                     if ln.strip() == "User comments section follows...")
    except StopIteration:
        return None
    fields = {}
    current = None
    for line in lines[start + 1:]:
        if not line[:1].isspace():
            break
        m = re.match(r"\s*([^\s=][^=]*)=(.*)$", line)
        if m:
            if m.group(1) not in fields:
                fields[m.group(1)] = m.group(2)
                current = m.group(1)
            else:
                current = None  # repeated field: keep first
        elif current is not None:
            fields[current] += "\n" + line
    return fields


def _vorbiscomment_tags(path):
    """Parse `vorbiscomment -l` (`KEY=value` lines, stored case).

    Lines without '=' rejoin the previous field (multiline values).
    """
    result = _run(["vorbiscomment", "-l", str(path)])
    if result.returncode != 0:
        return None
    fields = {}
    current = None
    for line in result.stdout.splitlines():
        m = re.match(r"([^=]+)=(.*)$", line)
        if m:
            if m.group(1) not in fields:
                fields[m.group(1)] = m.group(2)
                current = m.group(1)
            else:
                current = None  # repeated field: keep first
        elif current is not None:
            fields[current] += "\n" + line
    return fields
