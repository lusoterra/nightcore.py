#!/usr/bin/env python3

import argparse
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Codecs reported by ffprobe (stream codec_name, lowercased) that are
# lossy. Anything not in here is treated as lossless (no tag needed).
# This is intentionally conservative: unknown codecs get no tag rather
# than a wrong one.
LOSSY_CODECS = {
    "mp3",
    "mp2",
    "mp1",
    "aac",
    "aac_latm",
    "ac3",
    "eac3",
    "dts",
    "opus",
    "vorbis",
    "ogg",
    "speex",
    "wma",
    "wmav1",
    "wmav2",
    "wmapro",
    "mpc",
    "mpc7",
    "mpc8",
    "musepack",
    "amr_nb",
    "amr_wb",
    "amr-nb",
    "amr-wb",
    "gsm",
    "gsm_ms",
}

STANDARD_SAMPLE_RATES = [
    8000, 11025, 16000, 22050, 32000,
    44100, 48000, 88200, 96000,
    176400, 192000, 352800, 384000,
]

# Output formats: extension + encoder selection. Automatic quality defaults
# exist only for lossless output, tuned for most compression (FLAC level
# 12; ALAC/WAV have no knobs -- their defaults are already optimal).
# Lossy formats prefer the best native frontend (verified binary, ffmpeg
# fallback) but carry zero baked-in quality: --lossy-opts is mandatory
# for them (fail loudly otherwise), and rates are left alone unless -R
# is given -- the encoder choking on a weird rate is the user's signal
# to resample or retune it themselves.
# "wav" uses a depth-matched PCM codec (see wav_codec_for()).
OUTPUT_FORMATS = {
    "flac": {"ext": ".flac", "codec": "flac", "lossy": False,
             "ffmpeg_args": ["-compression_level", "12"]},
    "alac": {"ext": ".m4a", "codec": "alac", "lossy": False},
    "wav": {"ext": ".wav", "codec": None, "lossy": False},
    "mp3": {"ext": ".mp3", "codec": "libmp3lame", "lossy": True},
    "opus": {"ext": ".opus", "codec": "libopus", "lossy": True},
    "aac": {"ext": ".m4a", "codec": "aac", "lossy": True},
}

# Extensions scanned by --batch.
AUDIO_EXTS = (".flac", ".wav", ".aiff", ".aif", ".ape", ".wv", ".tta",
              ".alac", ".m4a", ".mp3", ".ogg", ".oga", ".opus", ".aac",
              ".wma", ".mpc", ".ac3", ".dts", ".amr", ".speex", ".mp2",
              ".lrc")


def _help_formatter(*args, **kwargs):
    kwargs.setdefault("max_help_position", 32)
    return argparse.RawTextHelpFormatter(*args, **kwargs)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Change audio speed/pitch using ffmpeg, sox and rubberband",
        formatter_class=_help_formatter
    )

    parser.add_argument(
        "input",
        type=str,
        nargs="?",
        default=None,
        help="Input audio file (not needed for --pitch-diff/--speed-diff;\n.lrc lyrics allowed with -s: retimed copy)"
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
            "(default: keep non-standard rate, bit-perfect)"
        )
    )

    parser.add_argument(
        "-B", "--both",
        action="store_true",
        default=False,
        help="With -R, also keep the non-standard-rate file"
    )

    parser.add_argument(
        "-F", "--format",
        choices=sorted(OUTPUT_FORMATS),
        default="flac",
        metavar="FMT",
        help="Output format: flac (default), alac, wav, mp3, opus, aac"
    )

    parser.add_argument(
        "--lossy-opts",
        type=str,
        default=None,
        metavar="OPTS",
        help=(
            "REQUIRED for lossy outputs: encoder quality flags\n"
            "(shlex-split, you choose, e.g. \"-q:a 0\" or \"--bitrate 128\").\n"
            "Flag dialect picks the encoder: native-CLI style (-V,\n"
            "--bitrate, ...) uses lame/opusenc/qaac, -X:a style uses ffmpeg\n"
            "(use the = form: --lossy-opts=\"-q:a 0\")"
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
        "-p", "--pitch",
        action="store_true",
        default=False,
        help=(
            "Pitch-shift only, keep duration (needs rubberband;\n"
            "a modifier on -s/-S: the amount comes from there)\n"
            "Labelled [Nst] full precision; negating reverses it"
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
        "--simple-title",
        type=str,
        nargs="?",
        const="",
        default=None,
        metavar="TEXT",
        help="Keep the Title tag free of script-added [...] groups\n"
             "(filenames keep them); optional TEXT is appended after\n"
             "a space, e.g. --simple-title=\"(Nightcore)\""
    )

    parser.add_argument(
        "--tag-template",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "Tag template file (key=\"value\" lines, multiline ok).\n"
            "Empty/bare values keep the source tag; tags missing\n"
            "from the file are stripped from outputs."
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
            "Undo from filename tags: reads [Nx]/[Nst], removes them\n"
            "Keeps quality tags; fails if no shift tags found"
        )
    )

    parser.add_argument(
        "--speed-diff",
        type=float,
        default=None,
        metavar="ST",
        help="Calculator, no conversion: semitones -> speed factor"
    )

    parser.add_argument(
        "--rb-params",
        type=str,
        default=None,
        metavar="PARAMS",
        help=(
            "Extra rubberband flags, appended after the defaults\n"
            "(-3 -p ST); a -p/--pitch inside overrides the pitch\n"
            "amount (warns) instead of duplicating it, e.g. --formant\n"
            "(use the = form: --rb-params=\"--formant\")"
        )
    )

    return parser.parse_args()


def parse_rb_params(params):
    """Split --rb-params into (extra_flags, pitch_override|None).

    Detects -p/--pitch in all its spellings (-p3, -p 3, --pitch 3,
    --pitch=3); the value overrides the computed pitch amount and is
    removed from the extras so rubberband never sees -p twice.
    Raises ValueError on bad quoting or a non-numeric pitch value.
    """
    if not params or not params.strip():
        return [], None
    try:
        tokens = shlex.split(params)
    except ValueError as e:
        raise ValueError(f"could not parse --rb-params: {e}")
    extra = []
    override = None
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if tok == "-p" or tok == "--pitch":
            if i + 1 >= len(tokens):
                raise ValueError("--rb-params -p needs a value")
            try:
                override = float(tokens[i + 1])
            except ValueError:
                raise ValueError(
                    f"--rb-params pitch value is not a number: {tokens[i + 1]!r}")
            skip_next = True
        elif tok.startswith("--pitch="):
            try:
                override = float(tok.split("=", 1)[1])
            except ValueError:
                raise ValueError(
                    f"--rb-params pitch value is not a number: {tok!r}")
        elif re.fullmatch(r"-p-?\d+(?:\.\d+)?", tok):
            try:
                override = float(tok[2:])
            except ValueError:
                raise ValueError(
                    f"--rb-params pitch value is not a number: {tok!r}")
        else:
            extra.append(tok)
    if override is not None and not math.isfinite(override):
        raise ValueError("--rb-params pitch value must be finite")
    return extra, override


def run_rubberband(input_wav, output_wav, semitones, extra_params):
    """Pitch-shift with rubberband (R3 engine); raises CalledProcessError."""
    run_tool(
        ["rubberband", "-3", "-p", repr(float(semitones))]
        + list(extra_params)
        + [str(input_wav), str(output_wav)],
    )


def get_sample_rate(input_path):
    try:
        ffprobe_cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(input_path)
        ]

        result = subprocess.run(
            ffprobe_cmd,
            capture_output=True,
            text=True,
            check=True
        )

        # Take first line only: guards against multi-stream output
        # (in case -select_streams is ignored by old ffprobe).
        first_line = result.stdout.strip().splitlines()
        if not first_line:
            return None

        try:
            return int(first_line[0].strip())
        except ValueError:
            print(
                f"Error: unexpected sample_rate from ffprobe: {first_line[0]!r}",
                file=sys.stderr,
            )
            return None

    except subprocess.CalledProcessError as e:
        print("ffprobe failed:", (e.stderr or "").strip(), file=sys.stderr)
        return None

    except FileNotFoundError:
        print("Error: ffprobe not found. Is ffmpeg installed?", file=sys.stderr)
        return None


def bits_from_stream(stream):
    """Integer bit depth from an ffprobe audio stream dict, or None.

    Prefers bits_per_raw_sample (e.g. 24-bit FLAC); falls back to
    sample_fmt mapping. Returns None for float/unknown (e.g. lossy
    decodes), where there is no integer depth to preserve.
    """
    try:
        bits = int(str(stream.get("bits_per_raw_sample")).strip())
        if bits > 0:
            return bits
    except (ValueError, AttributeError, TypeError):
        pass
    fmt = str(stream.get("sample_fmt") or "").lower()
    if fmt.startswith(("s32", "s64")):
        return 32
    if fmt.startswith("s16"):
        return 16
    if fmt.startswith(("u8", "s8")):
        return 8
    return None


def wav_codec_for(bits):
    """Depth-matched PCM encoder for .wav output (stays lossless)."""
    if bits is None:
        return "pcm_s16le"  # e.g. lossy source decoded to float
    if bits <= 16:
        return "pcm_s16le"
    if bits <= 24:
        return "pcm_s24le"
    return "pcm_s32le"


def get_audio_info(input_path):
    """Return (codec_name, bit_rate_bps, bits_per_sample) for first audio stream.

    codec_name is lowercased or None; bit_rate_bps is int or None (falls
    back to the format bit_rate when the stream bit_rate is missing);
    bits_per_sample is int or None (from bits_per_raw_sample, else mapped
    from sample_fmt; None for float/unknown, e.g. lossy decodes).
    """
    try:
        ffprobe_cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,bit_rate,sample_fmt,bits_per_raw_sample",
            "-show_entries", "stream_tags=encoder",
            "-show_entries", "format=bit_rate",
            "-show_entries", "format_tags=encoder",
            "-of", "json",
            str(input_path)
        ]

        result = subprocess.run(
            ffprobe_cmd,
            capture_output=True,
            text=True,
            check=True
        )

        info = json.loads(result.stdout or "{}")
        streams = info.get("streams") or []
        stream = streams[0] if streams else {}
        fmt = info.get("format") or {}

        codec = stream.get("codec_name")
        codec = str(codec).lower() if codec else None

        def _to_int(value):
            try:
                value = int(str(value).strip())
            except (ValueError, AttributeError, TypeError):
                return None
            return value if value > 0 else None

        bit_rate = _to_int(stream.get("bit_rate")) or _to_int(fmt.get("bit_rate"))
        format_bit_rate = _to_int(fmt.get("bit_rate"))

        stream_tags = stream.get("tags") or {}
        fmt_tags = fmt.get("tags") or {}
        raw_encoder = stream_tags.get("encoder") or fmt_tags.get("encoder")

        return codec, bit_rate, bits_from_stream(stream), format_bit_rate, raw_encoder

    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None, None, None, None, None


def mp3_header_info(path):
    """(mode, encoder) from MP3 Xing/Info/VBRI + LAME tags, stdlib only.

    mode is 'VBR', 'CBR', or None when no header is found (e.g. very old
    encoders). encoder is e.g. 'LAME3.100' or None. Never raises.
    """
    # Bitrate tables (kbps) and sample-rate table for Layer III.
    BR_M1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    BR_M2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
    RATES = {
        (3, 0): 44100, (3, 1): 48000, (3, 2): 32000,
        (2, 0): 22050, (2, 1): 24000, (2, 2): 16000,
        (0, 0): 11025, (0, 1): 12000, (0, 2): 8000,
    }
    try:
        with open(path, "rb") as f:
            head = f.read(32768)
    except OSError:
        return None, None
    if len(head) < 64:
        return None, None

    pos = 0
    if head[:3] == b"ID3":  # skip ID3v2 tag
        size = 0
        for b in head[6:10]:
            size = (size << 7) | (b & 0x7F)
        pos = 10 + size

    mode = None
    lame = None
    for _ in range(8):  # inspect first few frames
        i = head.find(b"\xff", pos, pos + 8192)
        if i < 0 or i + 4 > len(head) or head[i + 1] & 0xE0 != 0xE0:
            break
        b1, b2, b3 = head[i + 1], head[i + 2], head[i + 3]
        ver, br_idx, fr_idx = (b1 >> 3) & 0x03, (b2 >> 4) & 0x0F, (b2 >> 2) & 0x03
        if ver == 1 or (b1 >> 1) & 0x03 != 0x01 or br_idx in (0, 15) or fr_idx == 3:
            pos = i + 1
            continue
        mono = ((b3 >> 6) & 0x03) == 0x03
        br = (BR_M1 if ver == 3 else BR_M2)[br_idx] * 1000
        rate = RATES[(ver, fr_idx)]
        flen = int(((144 if ver == 3 else 72) * br / rate) + ((b2 >> 1) & 0x01))
        xing_at = i + 4 + (17 if mono else 32) if ver == 3 else i + 4 + (9 if mono else 17)
        magic = head[xing_at:xing_at + 4]
        if mode is None:
            if magic == b"Xing":
                mode = "VBR"
            elif magic == b"Info":
                mode = "CBR"
            elif head[i + 36:i + 40] == b"VBRI":  # Fraunhofer
                mode = "VBR"
        if lame is None and magic in (b"Xing", b"Info"):
            # LAME version sits 120 bytes into the Xing/Info header.
            # (Only the canonical position counts: muxers like Lavf write
            # their own Xing, and 'LAME..' byte runs can occur in audio data.)
            m = re.match(rb"LAME\d+\.\d+", head[xing_at + 120:xing_at + 132])
            if m:
                lame = m.group(0).decode("ascii")
        if mode is not None and lame is not None:
            break
        if flen < 21 or i + flen >= len(head):
            break
        pos = i + flen

    return mode, lame


def packet_vbr_info(path, threshold=0.02, min_packets=30, max_packets=300):
    """'VBR'/'CBR'/None from audio packet-size variance (demux-only scan).

    Constant-size packets mean CBR; varying sizes mean VBR. Returns None
    when there are too few packets to judge. Never raises.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "packet=size",
                "-of", "csv",
                "-read_intervals", f"%+#{max_packets}",
                str(path),
            ],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    sizes = []
    for line in result.stdout.splitlines():
        parts = line.split(",")
        if len(parts) >= 2:
            try:
                sizes.append(int(parts[1]))
            except ValueError:
                continue
    if len(sizes) < min_packets:
        return None
    mean = sum(sizes) / len(sizes)
    if mean <= 0:
        return None
    var = sum((s - mean) ** 2 for s in sizes) / len(sizes)
    return "CBR" if (var ** 0.5) / mean < threshold else "VBR"


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


_SHIFT_TOKEN = re.compile(r"^(-?\d+(?:\.\d+)?)(x|st)$")
_RATE_TOKEN = re.compile(r"^\d+(?:\.\d+)?kHz$")


def split_shift_tags(name):
    """Strip shift/rate/rb [...] groups; return (cleaned, speed, st, found, rb).

    Removes bracket groups made only of shift tokens ([1.2x], [3.15st],
    combined [1.2x 2.16st]), bare rate tags ([88.2kHz]), and recorded
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


# Bracket quality tags our own naming emits: "[16bit 44.1kHz]" and
# "[mp3 CBR 320k LAME3.100]" shapes (codec spellings cover the displayed
# dash-normalized variants too, e.g. "amr-nb").
_QUALITY_CODECS = {c.replace("_", "-") for c in LOSSY_CODECS} | LOSSY_CODECS
_BITRATE_TAG = re.compile(
    r"^\[(?:" + "|".join(sorted(_QUALITY_CODECS)) + r")"
    r"(?:\s+(?:VBR|CBR))?\s+\d+k(?:\s+.*)?\]$", re.IGNORECASE)
_DEPTH_TAG = re.compile(r"^\[\d+bit\s+\d+(?:\.\d+)?kHz\]$", re.IGNORECASE)


def strip_quality_rate_tags(name):
    """Remove quality/rate [...] groups; return the cleaned name.

    Removes bare rate tags ([88.2kHz], same rule as split_shift_tags)
    and quality tags in exactly the shapes this tool writes ([16bit
    44.1kHz], [mp3 CBR 320k LAME3.100]). Anything else stays verbatim,
    so real title brackets (e.g. [Greatest Hits], [Opus 1]) survive.
    Used for lossy outputs, which carry no source-quality or rate tags.
    """
    def _strip(m):
        toks = m.group(1).split()
        if toks and all(_RATE_TOKEN.match(t) for t in toks):
            return ""
        if _BITRATE_TAG.match(m.group(0)) or _DEPTH_TAG.match(m.group(0)):
            return ""
        return m.group(0)

    return " ".join(re.sub(r"\[([^\[\]]*)\]", _strip, name).split())


# LRC lyric timestamps: [mm:ss], [mm:ss.cc] (centiseconds) or [mm:ss.ccc]
# (milliseconds); the separator may be '.' or ':'. Metadata tags like
# [ar:Artist] never match (letters before the colon).
_LRC_STAMP = re.compile(r"\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\]")
_LRC_LENGTH = re.compile(
    r"^(\s*)\[length:(\d+):(\d{2})(?:[.:](\d{1,3}))?\](\s*)$",
    re.IGNORECASE)


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


# Source extensions preferred when pairing .lrc lyrics with their
# parent audio (same stem): lossless masters first, then anything else.
_LOSSLESS_EXTS = {".flac", ".alac", ".wav", ".aiff", ".aif",
                  ".ape", ".wv", ".tta"}


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
    """Retime-only .lrc lyrics job: scale timestamps, write a suffixed copy.

    Only -s (and -r, which folds into it) applies to lyrics; everything
    else is rejected loudly instead of silently ignored. Output is
    "stem [1.2x].lrc" next to the input (or under the batch out_dir),
    so one source can feed several sped-up audios.
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
    # Everything else (-F, --tag-template, -R/--both, --cover,
    # --rb-params, --lossy-opts) is accepted for naming fidelity: the
    # lyrics mirror the parent audio's output name, which those flags
    # shape. They change nothing about the lyrics themselves.
    if speed == 1.0:
        print("Error: nothing to do (speed is 1, .lrc needs -s to retime)",
              file=sys.stderr)
        return 1
    stem = predict_audio_stem(args, ctx, find_parent_audio(input_path))
    if stem is None:
        # No same-stem parent audio (or unreadable): fall back to the
        # plain shift-suffixed name.
        stem = sanitize_filename(f"{input_path.stem} [{speed:g}x]")
    output_path = ((out_dir if out_dir is not None else input_path.parent)
                   / (stem + ".lrc"))
    if args.dry_run:
        print(output_path)
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
        print(f"Error: cannot write {output_path}: {e}", file=sys.stderr)
        return 1
    return 0


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


def read_tags(input_path, *names):
    """Present format tags as {lowercased-name: stripped value}, one probe."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", f"format_tags={','.join(names)}",
                "-of", "json",
                str(input_path),
            ],
            capture_output=True, text=True, check=True,
        )
        tags = json.loads(result.stdout or "{}").get("format", {}).get("tags") or {}
        return {
            str(k).lower(): str(v).strip()
            for k, v in tags.items()
            if str(v).strip()
        }
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return {}


def read_format_tag(input_path, tag):
    """Single format tag value (stripped), or None if missing/unreadable."""
    try:
        ffprobe_cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", f"format_tags={tag}",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(input_path)
        ]

        result = subprocess.run(
            ffprobe_cmd,
            capture_output=True,
            text=True,
            check=True
        )

        value = result.stdout.strip()

        return value if value else None

    except (subprocess.CalledProcessError, FileNotFoundError):
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


def next_standard_rate(effective_rate):
    """Smallest standard rate >= effective_rate, or None if out of range."""
    for rate in STANDARD_SAMPLE_RATES:
        if rate >= effective_rate:
            return rate
    return None


def format_rate(rate):
    """Format Hz as kHz label, e.g. 96000 -> '96kHz', 88200 -> '88.2kHz'."""
    if rate % 1000 == 0:
        return f"{rate // 1000}kHz"
    return f"{rate / 1000:g}kHz"


def semitones_from_speed(speed):
    """Semitone equivalent of a speed/pitch factor: st = 12*log2(speed)."""
    return 12.0 * math.log2(speed)


def speed_from_semitones(semitones):
    """Speed/pitch factor for a semitone shift: speed = 2^(st/12)."""
    return 2.0 ** (semitones / 12.0)


def run_sox(input_path, output_path, effects):
    """Run one sox effect chain with progress; raises CalledProcessError."""
    run_tool(
        ["sox", "-S", str(input_path), str(output_path)] + effects,
    )


def missing_tools(*names):
    """Names of required executables not found on PATH."""
    return [n for n in names if shutil.which(n) is None]


# Ctrl+C handling: any press kills the running tool (if any) and exits
# cleanly at 130 with no traceback. Tools run detached (own session),
# so the terminal's SIGINT reaches only this process; the handler makes
# sure a tracked tool dies too instead of encoding on orphaned.
# Partial outputs tracked in _active_outputs are deleted; tempdirs
# clean themselves via finally.
_active_proc = None
_active_outputs = set()

# Set from -q/--quiet in main(): level 1 hushes tool spam (stages and
# announcements stay), level 2 (-qq) leaves only warnings and errors.
_quiet = 0


def say(msg):
    """Progress chatter for stderr; suppressed by -qq."""
    if _quiet < 2:
        print(msg, file=sys.stderr)


def _on_sigint(signum, frame):
    proc = _active_proc
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    raise KeyboardInterrupt


def run_tool(cmd):
    """Run an audio tool like subprocess.run(cmd, check=True), tracked.

    Detached session (a Ctrl+C handler kill reaches it via its process
    group) and registered in _active_proc for that kill. Raises
    subprocess.CalledProcessError on nonzero exit, same as check=True.
    At quiet level 1+ the tool's output is captured and only shown when
    it fails.
    """
    global _active_proc
    if _quiet >= 1:
        proc = subprocess.Popen(cmd, start_new_session=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    else:
        proc = subprocess.Popen(cmd, start_new_session=True)
    _active_proc = proc
    try:
        if _quiet >= 1:
            out, _ = proc.communicate()
            returncode = proc.returncode
        else:
            returncode = proc.wait()
    finally:
        _active_proc = None
    if returncode != 0:
        if _quiet >= 1 and out:
            sys.stderr.buffer.write(out)
            sys.stderr.buffer.flush()
        raise subprocess.CalledProcessError(returncode, cmd)


def _verified_cli(binary, check_args, marker, timeout=60):
    """PATH + version-output check for an external encoder; bool."""
    if shutil.which(binary) is None:
        return False
    try:
        result = subprocess.run(
            [binary] + check_args,
            capture_output=True, text=True, check=False, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0 and marker in output


_qaac_version = None


def sample_is_float(path):
    """True if audio decodes as float (or the probe fails: convert anyway)."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=sample_fmt",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return True
    fmt = (result.stdout or "").strip().lower()
    return not fmt or fmt.startswith(("flt", "dbl"))


def find_qaac():
    """Verify a genuine qaac binary is usable; return its version line or None.

    Checks PATH plus `qaac --check` output for the qaac/CoreAudioToolbox
    markers, so unrelated same-named executables don't qualify. Result is
    cached for the run (wine startup isn't free).
    """
    global _qaac_version
    if _qaac_version is False:
        return None
    if _qaac_version is not None:
        return _qaac_version
    _qaac_version = False
    if shutil.which("qaac") is None:
        return None
    try:
        result = subprocess.run(
            ["qaac", "--check"],
            capture_output=True, text=True, check=False, timeout=180,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    output = (result.stdout or "") + (result.stderr or "")
    first_line = output.strip().splitlines()[0] if output.strip() else ""
    if result.returncode == 0 and first_line.startswith("qaac ") \
            and "CoreAudioToolbox" in output:
        _qaac_version = first_line
        return _qaac_version
    return None


def find_lame():
    """True if a genuine lame binary is usable."""
    return _verified_cli("lame", ["--version"], "LAME")


def find_opusenc():
    """True if a genuine opusenc binary is usable."""
    return _verified_cli("opusenc", ["--version"], "opusenc")


# Bare short flags that only make sense to the native CLI frontend.
# (Any --long-opt also votes CLI; any -X:a stream option or ffmpeg-only
# name votes ffmpeg; anything else, e.g. -ar/-ac, is ffmpeg-side too --
# the CLI tools have no such flags. See README for the dialect table.)
CLI_SHORT_OPTS = {
    "mp3": {"-V", "-q", "-b", "-m"},
    "opus": set(),
    "aac": {"-V", "-v", "-c", "-a", "-q"},
}
FFMPEG_ONLY_OPTS = {
    "-ar", "-ac", "-ab", "-vbr", "-application", "-frame_duration",
    "-packet_loss", "-mapping_family", "-aac_coder", "-aac_ms", "-aac_is",
    "-aac_pns", "-cutoff",
}

_DIALECT_EXAMPLES = {
    "mp3": ('lame-style ("-V 0")', 'libmp3lame-style ("-q:a 0")'),
    "opus": ('opusenc-style ("--bitrate 128")', 'libopus-style ("-b:a 128k")'),
    "aac": ('qaac-style ("-V 91")', 'ffmpeg-style ("-b:a 128k")'),
}


def detect_lossy_dialect(fmt, opts):
    """'cli', 'ffmpeg', or None (no recognizable flags) for --lossy-opts.

    Raises ValueError when opts mix both dialects: the flags would only
    work on one backend, so guessing would misroute them.
    """
    cli = any(t in CLI_SHORT_OPTS.get(fmt, ())
              or (t.startswith("--") and len(t) > 2) for t in opts)
    ff = any(t in FFMPEG_ONLY_OPTS
             or re.match(r"^-[A-Za-z]+:a(?::\d+)?$", t) for t in opts)
    if cli and ff:
        native, ffmpeg = _DIALECT_EXAMPLES[fmt]
        raise ValueError(
            f"--lossy-opts mixes {native} and {ffmpeg} flags; "
            f"pick one dialect (one encoder)")
    if cli:
        return "cli"
    if ff:
        return "ffmpeg"
    return None


def select_lossy_backend(fmt, dialect):
    """Pick (and announce) the encoder backend for a lossy format.

    dialect is None (default preference order) or "cli"/"ffmpeg" forced
    by --lossy-opts flag dialect. A forced backend that is unavailable
    fails loudly instead of falling back (its flags wouldn't work
    elsewhere). Quality-fallback warnings only fire on the default path:
    an explicit dialect means the user knows what they chose.
    Returns {"kind": ...} or None after printing an error. Notices go to
    stderr once per run, not per file, for --batch sanity. kind is one of
    "ffmpeg" (in-process codec), "lame", "opusenc" or "qaac".
    """
    forced = " (via --lossy-opts)" if dialect else ""
    if fmt == "mp3":
        if dialect == "cli":
            if not find_lame():
                print("Error: --lossy-opts are lame-style but no lame "
                      "binary found (install lame)", file=sys.stderr)
                return None
            say(f"Encoding MP3 with lame CLI{forced}")
            return {"kind": "lame"}
        if dialect == "ffmpeg" and not ffmpeg_has_encoder("libmp3lame"):
            print("Error: --lossy-opts are libmp3lame-style but this "
                  "ffmpeg lacks libmp3lame", file=sys.stderr)
            return None
        if ffmpeg_has_encoder("libmp3lame"):
            say(f"Encoding MP3 with libmp3lame{forced}")
            return {"kind": "ffmpeg"}
        if find_lame():
            say(f"Encoding MP3 with lame CLI{forced}")
            return {"kind": "lame"}
        print("Error: no MP3 encoder found "
              "(need ffmpeg with libmp3lame, or lame)", file=sys.stderr)
        return None
    if fmt == "opus":
        if dialect == "cli":
            if not find_opusenc():
                print("Error: --lossy-opts are opusenc-style but no "
                      "opusenc binary found (install opus-tools)",
                      file=sys.stderr)
                return None
            say(f"Encoding Opus with opusenc{forced}")
            return {"kind": "opusenc"}
        if dialect == "ffmpeg" and not ffmpeg_has_encoder("libopus"):
            print("Error: --lossy-opts are libopus-style but this "
                  "ffmpeg lacks libopus", file=sys.stderr)
            return None
        if dialect is None and find_opusenc():
            say("Encoding Opus with opusenc")
            return {"kind": "opusenc"}
        if ffmpeg_has_encoder("libopus"):
            if dialect is None:
                print("Warning: opusenc not found, Opus via ffmpeg libopus "
                      "sounds worse (install opus-tools for quality)",
                      file=sys.stderr)
            else:
                say(f"Encoding Opus with ffmpeg libopus{forced}")
            return {"kind": "ffmpeg"}
        if find_opusenc():
            say(f"Encoding Opus with opusenc{forced}")
            return {"kind": "opusenc"}
        print("Error: no Opus encoder found "
              "(need opusenc, or ffmpeg with libopus)", file=sys.stderr)
        return None
    if fmt == "aac":
        version = find_qaac()
        if dialect == "cli":
            if version is None:
                print("Error: --lossy-opts are qaac-style but no qaac "
                      "binary found (install qaac)", file=sys.stderr)
                return None
            say(f"Encoding AAC with {version}{forced}")
            return {"kind": "qaac"}
        if dialect == "ffmpeg" and not ffmpeg_has_encoder("aac"):
            print("Error: --lossy-opts are ffmpeg-style but this "
                  "ffmpeg lacks aac", file=sys.stderr)
            return None
        if dialect is None and version is not None:
            say(f"Encoding AAC with {version}")
            return {"kind": "qaac"}
        if ffmpeg_has_encoder("aac"):
            if dialect is None:
                print("Warning: qaac not found, AAC via ffmpeg sounds worse "
                      "(use qaac for quality)", file=sys.stderr)
            else:
                say(f"Encoding AAC with ffmpeg{forced}")
            return {"kind": "ffmpeg"}
        if version is not None:
            say(f"Encoding AAC with {version}{forced}")
            return {"kind": "qaac"}
        print("Error: no AAC encoder found "
              "(need qaac, or ffmpeg with aac)", file=sys.stderr)
        return None
    return None


def ffmpeg_has_encoder(name):
    """True if this ffmpeg build includes the named audio encoder."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", f"encoder={name}"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def run_lame(input_wav, output_mp3, extra_args):
    """Encode MP3 with lame (same lib as libmp3lame); audio only.

    Quality flags come from --lossy-opts (mandatory for lossy outputs).
    """
    run_tool(
        ["lame"] + list(extra_args)
        + [str(input_wav), str(output_mp3)],
    )


def run_opusenc(input_wav, output_opus, tags, cover_file, extra_args):
    """Encode Opus with opusenc, fully tagged (ffmpeg can't write opus
    pictures, so tags go here natively, not through a later pass)."""
    # Quality flags come from --lossy-opts (mandatory for lossy outputs).
    cmd = ["opusenc"] + list(extra_args) + [
        "--discard-comments", "--discard-pictures",
    ]
    for flag, key in (("--title", "title"), ("--artist", "artist"),
                      ("--album", "album"), ("--genre", "genre"),
                      ("--date", "date")):
        if tags.get(key):
            cmd += [flag, tags[key]]
    track = tags.get("track")
    if track and re.fullmatch(r"\d+(/\d+)?", str(track)):
        cmd += ["--tracknumber", str(track).split("/")[0]]
    handled = {"title", "artist", "album", "genre", "date", "track",
               "_extra"}
    for key, value in tags.items():
        if key in handled or not value:
            continue
        cmd += ["--comment", f"{key}={value}"]
    for key, value in tags.get("_extra", {}).items():
        cmd += ["--comment", f"{key}={value}"]
    if cover_file is not None:
        cmd += ["--picture", str(cover_file)]
    cmd += [str(input_wav), str(output_opus)]
    run_tool(cmd)


def run_qaac_alac(input_wav, output_m4a, alac=True, extra_args=()):
    """Encode ALAC (or AAC) with qaac (CoreAudio); audio only.

    input_wav must be integer PCM (qaac rejects float). All metadata and
    cover art goes through ffmpeg afterwards: qaac under wine is a hack
    this script uses strictly as an AAC/ALAC engine, never for tagging.
    extra_args (e.g. TVBR tuning for AAC) go before the output flag.
    Raises CalledProcessError on failure.
    """
    cmd = ["qaac"]
    cmd += ["--alac"] if alac else []
    cmd += list(extra_args)
    cmd += ["--rate", "keep", "-o", str(output_m4a), str(input_wav)]
    run_tool(cmd)


def cover_codec_name(path):
    """Video codec of the first attached picture stream, or None."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v",
                "-show_entries", "stream=codec_name",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    for line in result.stdout.splitlines():
        name = line.strip().lower()
        if name:
            return name
    return None


# Attached-picture codecs safe to pass through (ffmpeg copy) or hand to qaac.
COVER_CODECS = {"mjpeg": "jpg", "png": "png"}

# Image file suffixes and their MIME types (for --cover validation and
# metaflac picture imports).
_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def flac_postprocess(flac_path, image, mime, comment, description, encoder):
    """Fix FLAC metadata ffmpeg gets wrong; warn per file, never raises.

    - image/mime: replace all pictures with this file as Cover (front)
      (ffmpeg never propagates picture types, readers show Other).
    - comment/description: rewrite exactly (ffmpeg merges COMMENT into
      DESCRIPTION on write). None means leave alone.
    - encoder: explicit value via metaflac (ffmpeg stamps its own or,
      under bitexact, nothing at all).
    Reports failure per file with a warning, never raises. (Callers
    pre-check for a metaflac binary and refuse to run when these fixes
    are required but unavailable.)
    """
    calls = []
    if image is not None:
        calls.append(["--remove", "--block-type=PICTURE"])
        calls.append([f"--import-picture-from=3|{mime}|||{image}"])
    tag_ops = []
    if comment is not None or description is not None:
        tag_ops += ["--remove-tag=COMMENT", "--remove-tag=DESCRIPTION"]
    if comment is not None:
        tag_ops.append(f"--set-tag=COMMENT={comment}")
    if description is not None:
        tag_ops.append(f"--set-tag=DESCRIPTION={description}")
    if encoder is not None:
        tag_ops.append(f"--set-tag=ENCODER={encoder}")
    if tag_ops:
        calls.append(tag_ops)
    ok = True
    for op in calls:
        try:
            subprocess.run(["metaflac"] + op + [str(flac_path)],
                           check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            ok = False
    if not ok:
        print(f"Warning: metaflac fixes failed on {flac_path}", file=sys.stderr)
    return ok

# Source encoder tags that must not survive a lossy re-encode: they
# describe how the SOURCE was made, not the output (ffmpeg's own fresh
# encoder tag, if any, is unaffected).
STALE_TAGS = ("encoder", "encoding_settings")


def read_all_tags(input_path):
    """All format + first-audio-stream tags as (fmt_dict, stream_dict).

    Keys keep their original case; empty values are dropped. Returns
    ({}, {}) when unreadable. Never raises.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "format_tags",
                "-show_entries", "stream_tags",
                "-of", "json",
                str(input_path),
            ],
            capture_output=True, text=True, check=True,
        )
        info = json.loads(result.stdout or "{}")
        fmt = info.get("format") or {}

        def _clean(tags):
            return {str(k): str(v) for k, v in (tags or {}).items()
                    if str(v) != ""}

        streams = info.get("streams") or []
        stream = streams[0] if streams else {}
        return _clean(fmt.get("tags")), _clean(stream.get("tags"))
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return {}, {}


# Template key spellings some muxers don't store (movenc drops bare
# "albumartist" but keeps "album_artist").
_TAG_ALIASES = {"albumartist": "album_artist"}


def run_ffmpeg(input_path, output_path, audio_filter, codec, title, overwrite,
               metadata_from=None, sample_fmt=None, with_cover=False,
               cover_file=None, strip_tags=False, extra_tags=None,
               ffmpeg_args=None, drop_video=False, drop_tags=None):
    """Run one ffmpeg transcode; raises CalledProcessError on failure.

    metadata_from: optional extra input whose global metadata is mapped
    to the output (used to restore tags lost by intermediate sox steps);
    the explicit title always wins. Audio is always taken from input_path.
    sample_fmt: optional forced output depth (s16/s24/s32); None leaves
    the encoder's default negotiation (used for lossless temps/sources).
    with_cover: also map the attached picture + stream tags from
    metadata_from (ffmpeg flac/m4a finals on the sox path).
    cover_file: attach this image as the sole picture instead (drops all
    others); direct encodes then map 0:a explicitly to keep their default
    tag behavior. Combines with metadata_from (cover becomes input 2).
    strip_tags: drop every tag first, then write extra_tags + title
    (tag templates).
    drop_tags: tag names (case-insensitive) to exclude while mapping
    (lossy outputs drop stale source encoder tags); implemented as an
    explicit pass-through of everything else.
    """
    ffmpeg_cmd = [
        "ffmpeg",
        "-nostdin",
        "-y" if overwrite else "-n",
        "-i", str(input_path),
    ]
    if metadata_from is not None:
        ffmpeg_cmd += ["-i", str(metadata_from)]
    if cover_file is not None:
        ffmpeg_cmd += ["-i", str(cover_file)]
    if metadata_from is not None:
        ffmpeg_cmd += ["-map", "0:a"]
        if not drop_tags:
            ffmpeg_cmd += ["-map_metadata", "1",
                           "-map_metadata:s:a", "1:s:a"]
        if with_cover:
            ffmpeg_cmd += ["-map", "1:v", "-c:v", "copy",
                           "-disposition:v", "attached_pic"]
        if cover_file is not None:
            # --cover wins outright, even alongside restored metadata
            # (cover input sits at index 2 here, 1 on direct encodes).
            ffmpeg_cmd += ["-map", "2:v",
                           "-c:v", "copy", "-disposition:v", "attached_pic"]
    elif cover_file is not None:
        ffmpeg_cmd += ["-map", "0:a", "-map", "1:v",
                       "-c:v", "copy", "-disposition:v", "attached_pic"]
        if not strip_tags and not drop_tags:
            # (skipped under --tag-template: strip-all already follows and
            # would lose to the merge; explicit tags are added below anyway.
            # Also skipped for drop_tags: the explicit pass-through below
            # starts from a clean slate instead.)
            ffmpeg_cmd += ["-map_metadata", "0", "-map_metadata:s:a", "0:s:a"]
    elif drop_video:
        # Some muxers (ogg-opus) reject video streams outright: audio only.
        ffmpeg_cmd += ["-map", "0:a"]
    if strip_tags or drop_tags:
        ffmpeg_cmd += ["-map_metadata", "-1", "-map_metadata:s:a", "-1"]
    if drop_tags and not strip_tags:
        # Explicit pass-through minus stale tags (a blind -map_metadata
        # would copy them). Source is the metadata input when present,
        # else the audio input itself (ffmpeg's default mapping).
        tag_source = (metadata_from if metadata_from is not None
                      else input_path)
        fmt_tags, stream_tags = read_all_tags(tag_source)
        banned = {t.lower() for t in drop_tags}
        for key, value in fmt_tags.items():
            if key.lower() not in banned:
                ffmpeg_cmd += ["-metadata", f"{key}={value}"]
        for key, value in stream_tags.items():
            if key.lower() not in banned:
                ffmpeg_cmd += ["-metadata:s:a", f"{key}={value}"]
    for key, value in (extra_tags or {}).items():
        ffmpeg_cmd += ["-metadata", f"{key}={value}"]
    if audio_filter is not None:
        ffmpeg_cmd += ["-af", audio_filter]
    if sample_fmt is not None:
        ffmpeg_cmd += ["-sample_fmt", sample_fmt]
    ffmpeg_cmd += [
        "-c:a", codec,
    ]
    # Encoder tuning (bitrate/quality defaults, then user --lossy-opts).
    ffmpeg_cmd += list(ffmpeg_args or [])
    ffmpeg_cmd += [
        "-metadata", f"title={title}",
        # Bitexact output: the muxer writes no encoder/version tags of its
        # own, so stripped tags stay stripped (a template `encoder` entry
        # still applies, as do all explicit -metadata values above).
        "-fflags", "+bitexact",
        str(output_path)
    ]
    run_tool(ffmpeg_cmd)


def main():
    signal.signal(signal.SIGINT, _on_sigint)
    args = parse_arguments()
    global _quiet
    _quiet = args.quiet

    if args.pitch_diff is not None:
        vals = args.pitch_diff
        if len(vals) == 1:
            (factor,) = vals
            if factor <= 0 or not math.isfinite(factor):
                print("Error: --pitch-diff speed factor must be finite and > 0",
                      file=sys.stderr)
                return 1
            st = 12.0 * math.log2(factor)
            print(f"speed {factor:g}x = {st!r}st ({st * 100!r} cents)")
        elif len(vals) == 2:
            f1, f2 = vals
            if (f1 <= 0 or f2 <= 0
                    or not math.isfinite(f1) or not math.isfinite(f2)):
                print("Error: --pitch-diff frequencies must be finite and > 0",
                      file=sys.stderr)
                return 1
            st = 12.0 * math.log2(f2 / f1)
            print(f"{f2:g} Hz vs {f1:g} Hz: {st!r}st ({st * 100!r} cents), "
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

    missing = missing_tools("ffmpeg", "ffprobe")
    if missing:
        print(
            f"Error: required tools not found on PATH: {', '.join(missing)}.\n"
            "Install FFmpeg (provides ffmpeg + ffprobe) and re-run.",
            file=sys.stderr,
        )
        return 127

    # -S alone (or with -p) is pitch-only; -s + -S together is one
    # combined speedup-then-pitch job: bit-perfect asetrate first, then
    # SoX upsamples by the semitone factor and rubberband pitch-shifts.
    # A -p/--pitch inside --rb-params overrides the semitone amount
    # exactly as if -S had been given it (warned about below).
    rb_extras, rb_pitch = [], None
    if args.rb_params is not None:
        try:
            rb_extras, rb_pitch = parse_rb_params(args.rb_params)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    eff_semitones = rb_pitch if rb_pitch is not None else args.semitones
    if rb_pitch is not None and (args.semitones is not None
                                 or (args.pitch and args.speed != 1.0)):
        print(f"Warning: --rb-params -p {rb_pitch:g} overrides the pitch amount",
              file=sys.stderr)
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

    if args.both and not args.resample:
        print("Error: --both requires -R/--resample", file=sys.stderr)
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

    backend = None
    if is_lossy_format:
        try:
            dialect = detect_lossy_dialect(args.format, lossy_opts)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        backend = select_lossy_backend(args.format, dialect)
        if backend is None:
            return 127
        if not lossy_opts:
            kind = backend.get("kind")
            if args.format == "mp3":
                tool = "libmp3lame" if kind == "ffmpeg" else "lame"
                hint = '"-q:a 0"' if kind == "ffmpeg" else '"-V 0"'
            elif args.format == "opus":
                tool = "opusenc" if kind == "opusenc" else "libopus"
                hint = '"--bitrate 128"' if kind == "opusenc" else '"-b:a 128k"'
            else:
                tool = "qaac" if kind == "qaac" else "ffmpeg AAC"
                hint = '"-V 91"' if kind == "qaac" else '"-b:a 128k"'
            print(f"Error: -F {args.format} via {tool} needs --lossy-opts "
                  f"(you choose the quality, no defaults), "
                  f"e.g. --lossy-opts {hint}", file=sys.stderr)
            return 1

    ctx = {
        "tmpl": tmpl,
        "cover_flag": cover_flag,
        "lossy_opts": lossy_opts,
        "rb_extras": rb_extras,
        "rb_pitch": rb_pitch,
        "backend": backend,
        "speed": speed,
        "combo": combo,
        "st_signed": st_signed,
        "combo_seq": combo_seq,
        "eff_semitones": eff_semitones,
    }

    if args.batch is not None:
        if args.input:
            print("Error: --batch takes a directory, not a file "
                  "(drop the positional input)", file=sys.stderr)
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
        out_base = Path.cwd() / "encodes" / args.format
        if not args.dry_run:
            out_base.mkdir(parents=True, exist_ok=True)
        failed = []
        for candidate in files:
            if run_conversion(args, candidate, out_base, ctx) != 0:
                failed.append(candidate.name)
        if failed:
            print(f"Error: {len(failed)} of {len(files)} file(s) failed: "
                  + ", ".join(failed), file=sys.stderr)
            return 1
        verb = "Would convert" if args.dry_run else "Done"
        skipped = ctx.get("skipped", 0)
        note = f" ({skipped} skipped, use -y to re-encode)" if skipped else ""
        print(f"{verb}: {len(files)} file(s){note} -> {out_base}")
        return 0
    if not args.input:
        print("Error: input file is required", file=sys.stderr)
        return 1
    return run_conversion(args, Path(args.input), None, ctx)


def run_conversion(args, input_path, out_dir, ctx):
    """Convert one file. out_dir overrides the output directory (batch).

    ctx carries run-global values: tmpl, cover_flag, lossy_opts,
    rb_extras, rb_pitch, backend, speed, combo, st_signed, combo_seq,
    eff_semitones.
    Returns a process-style exit code.
    """
    tmpl = ctx["tmpl"]
    cover_flag = ctx["cover_flag"]
    lossy_opts = ctx["lossy_opts"]
    rb_extras = ctx["rb_extras"]
    rb_pitch = ctx["rb_pitch"]
    backend = ctx["backend"]
    speed = ctx["speed"]
    combo = ctx["combo"]
    st_signed = ctx["st_signed"]
    combo_seq = ctx["combo_seq"]
    eff_semitones = ctx["eff_semitones"]

    input_path = Path(input_path)
    if not input_path.is_file():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        return 1

    if input_path.suffix.lower() == ".lrc":
        return run_lrc(args, input_path, out_dir, ctx)

    # -D reads the shift back out of the filename tags ([1.2x], [3.15st],
    # combined [1.2x 2.16st]) instead of taking -s/-S/-p/-r.
    d_speed, d_st, d_seq = 1.0, 0.0, 1.0
    d_rb = None
    cleaned_stem = None
    if args.denightcore:
        for flag, bad in (("-s/--speed", args.speed != 1.0),
                          ("-S/--semitones", eff_semitones is not None),
                          ("-p/--pitch", args.pitch),
                          ("-r/--reciprocal", args.reciprocal),
                          ("--rb-params -p", rb_pitch is not None)):
            if bad:
                print(f"Error: --denightcore cannot be combined with {flag} "
                      "(it reads the shift from filename tags)", file=sys.stderr)
                return 1
        cleaned_stem, d_speed, d_st, found, d_rb = split_shift_tags(
            input_path.stem)
        if not found:
            print(f"Error: no [Nx] or [Nst] shift tags in '{input_path.name}'; "
                  "--denightcore reads the amount from filename tags like "
                  "[1.2x] or [3.15st], e.g. nightcore 'Song [1.2x].flac' -D",
                  file=sys.stderr)
            return 1
        speed = 1.0 / d_speed
        d_seq = speed_from_semitones(d_st)
    naming = resolve_audio_naming(
        args, tmpl, rb_extras, input_path, speed,
        combo, st_signed, combo_seq, eff_semitones,
        denight=(None if not args.denightcore else {
            "d_speed": d_speed, "d_st": d_st, "d_seq": d_seq,
            "d_rb": d_rb, "cleaned_stem": cleaned_stem,
        }))
    if naming is None:
        print("Error: could not determine input sample rate", file=sys.stderr)
        return 1
    speed = naming["speed"]
    sample_rate = naming["sample_rate"]
    target_rate = naming["target_rate"]
    is_lossy_output = naming["is_lossy_output"]
    bits = naming["bits"]
    pitch_mode = naming["pitch_mode"]
    pitch_rb = naming["pitch_rb"]
    weird_suffix = naming["weird_suffix"]
    standard_suffix = naming["standard_suffix"]
    cleaned_stem = naming["cleaned_stem"]
    out_ext = naming["out_ext"]
    out_codec = naming["out_codec"]
    depth_fmt = naming["depth_fmt"]
    depth_pcm = naming["depth_pcm"]
    artist = naming["artist"]
    original_title = naming["original_title"]
    title_base = naming["title_base"]
    ff_extra = naming["ff_extra"]
    stub_encoder = naming["stub_encoder"]

    if (not args.denightcore and not pitch_mode and speed == 1.0
            and target_rate is None
            and input_path.suffix.lower() == out_ext
            and tmpl is None and cover_flag is None):
        print(
            "Error: nothing to do (speed is 1 and no resampling requested)",
            file=sys.stderr,
        )
        return 1
    # e.g. wav -> flac with speed 1: still convert to target format.

    if args.both and target_rate is None and not is_lossy_output:
        # Requested both outputs but resampling is a no-op (already a
        # standard rate, or out of range): there is only one distinct file.
        print(
            "Warning: --both requested but resampling is a no-op; "
            "generating only the weird-rate file.",
            file=sys.stderr,
        )

    def weird_filter():
        # Speedup is a pure rate reinterpretation: no resampling, ever.
        # (Resampling happens exclusively in SoX, only for -R outputs
        # and the pitch-subrecipe rate step.)
        if speed == 1.0:
            # Pure container/codec conversion (e.g. mp3 -> flac): no filter.
            return None
        return f"asetrate={sample_rate}*{speed}"

    # Job list: (suffix, plan). --both emits the weird-rate file first,
    # then the standard-rate file. Plans:
    #   ("direct", audio_filter)                        - ffmpeg only (weird speedup)
    #   ("resample", prefilter, target)                 - ffmpeg -> sox rate -> ffmpeg
    #   ("pitch", prefilter, pitched_rate, st, target, rb) - ffmpeg -> sox rate -> rubberband -> ffmpeg
    # Temp WAVs are f32 (exact for <=24-bit int and float32 sources).
    jobs: list[tuple[str, Any]]
    if pitch_mode:
        # d_seq folds a -D pitch inversion into the same math (1.0 otherwise).
        if combo:
            pitch_st = st_signed
        else:
            pitch_st = (-d_st if args.denightcore and d_st != 0.0
                        else semitones_from_speed(speed / d_seq))
        # The SoX rate step always scales by the pitch factor from the
        # post-prefilter rate: headroom for shifted highs, exact rate.
        base_rate = (sample_rate * speed
                     if (args.denightcore or combo) else sample_rate)
        pitched_rate = round(base_rate * speed_from_semitones(pitch_st))
        # -D combined undo slows down first via asetrate; combo speeds up
        # first the same way (plain pitch jobs decode unfiltered).
        prefilter = weird_filter() if (args.denightcore or combo) else None
        if args.both and target_rate is not None:
            jobs = [
                (weird_suffix, ("pitch", prefilter, pitched_rate, pitch_st, None, pitch_rb)),
                (standard_suffix, ("pitch", prefilter, pitched_rate, pitch_st, target_rate, pitch_rb)),
            ]
        elif target_rate is not None:
            jobs = [(standard_suffix, ("pitch", prefilter, pitched_rate, pitch_st, target_rate, pitch_rb))]
        else:
            jobs = [(weird_suffix, ("pitch", prefilter, pitched_rate, pitch_st, None, pitch_rb))]
    elif args.both and target_rate is not None:
        jobs = [
            (weird_suffix, ("direct", weird_filter())),
            # Prefilter is speedup-only: SoX alone performs the resampling.
            (standard_suffix, ("resample", weird_filter(), target_rate)),
        ]
    elif target_rate is not None:
        jobs = [(standard_suffix, ("resample", weird_filter(), target_rate))]
    else:
        jobs = [(weird_suffix, ("direct", weird_filter()))]

    pitch_plans = [plan for _, plan in jobs if plan[0] == "pitch"]

    def _needs_sox():
        # Mirrors the executor below: SoX runs for every resample plan,
        # for pitch-up pre-scales, and for pitch-down settles/targets.
        for _, plan in jobs:
            if plan[0] == "resample":
                return True
            if plan[0] == "pitch":
                _, prefilter, pitched_rate, pitch_st, target, _ = plan
                cur_rate = (round(sample_rate * speed)
                            if prefilter is not None else sample_rate)
                if pitch_st >= 0:
                    if pitched_rate != cur_rate:
                        return True
                elif target is not None or pitched_rate != cur_rate:
                    return True
        return False

    if not args.dry_run and pitch_plans and shutil.which("rubberband") is None:
        print(
            "Error: 'rubberband' not found. Pitch shifting (-p, -S, -D pitch)\n"
            "requires rubberband installed (apt/pacman/brew install rubberband).",
            file=sys.stderr,
        )
        return 127

    # SoX runs for every resample plan, for pitch-up pre-scales, and for
    # pitch-down settles/targets; rubberband only ever shifts pitch.
    if (not args.dry_run and _needs_sox()
            and shutil.which("sox") is None):
        print(
            "Error: 'sox' not found. Resampling (-R) and pitch rate steps\n"
            "require SoX installed (apt/pacman/brew install sox).",
            file=sys.stderr,
        )
        return 127

    if args.rb_params and not pitch_plans:
        print("Warning: --rb-params has no effect without pitch "
              "(no pitch job in this run)", file=sys.stderr)

    # Output filenames: "Artist - Title [speed|st] [<codec> <bitrate>k]
    # [<rate>]<ext>" (artist prefix omitted when the source has no artist
    # tag; "Title" is the embedded title, falling back to the filename).
    # The Title tag is the same minus the artist prefix. Names are
    # sanitized for Linux/Windows-safe filenames. Preserve input directory.
    # -D instead strips shift/rate tags off the input's own name and title,
    # keeping quality tags verbatim.
    artist_prefix = f"{artist} - " if artist else ""
    if args.denightcore:
        original_title = split_shift_tags(original_title)[0]
        name_base = cleaned_stem
    else:
        name_base = artist_prefix + original_title
    outputs = [
        (
            suffix,
            (out_dir if out_dir is not None else input_path.parent)
            / (sanitize_filename(name_base + suffix) + out_ext),
        )
        for suffix, _ in jobs
    ]

    if out_dir is not None:
        # Batch mode funnels many inputs into one directory, so
        # tag-derived names can collide (duplicate files with identical
        # tags, or lossy outputs whose quality tags are stripped). The
        # first file keeps the name; later ones get " (2)", " (3)", ...
        claimed = ctx.setdefault("claimed_outputs", set())
        deduped = []
        for suffix, output_path in outputs:
            candidate = output_path
            n = 2
            while candidate in claimed:
                candidate = output_path.with_name(
                    f"{output_path.stem} ({n}){output_path.suffix}")
                n += 1
            claimed.add(candidate)
            deduped.append((suffix, candidate))
        outputs = deduped

    for suffix, output_path in outputs:
        if output_path == input_path:
            print("Error: output would overwrite input, aborting", file=sys.stderr)
            return 1

    if args.dry_run:
        for _, output_path in outputs:
            print(output_path)
        return 0

    for suffix, output_path in outputs:
        if output_path.exists() and not args.overwrite:
            if out_dir is not None and all(
                    p.exists() for _, p in outputs):
                # Batch rerun without -y: skip what's already encoded
                # (add files freely; -y re-encodes everything).
                print(f"Skipping {input_path.name} (output exists, "
                      "use -y to re-encode)", file=sys.stderr)
                ctx["skipped"] = ctx.get("skipped", 0) + 1
                return 0
            print(
                f"Error: output already exists: {output_path} (use -y to overwrite)",
                file=sys.stderr,
            )
            return 1

    try:
        total = len(jobs)
        # qaac (CoreAudio, often run via wine) encodes ALAC when present;
        # otherwise ffmpeg's alac encoder is used. qaac needs integer PCM.
        # AAC also prefers qaac (announced once in main); its fallback
        # warning already fired there.
        be_kind = backend.get("kind") if backend else None
        qaac_version = find_qaac() if args.format == "alac" else None
        use_qaac = (qaac_version is not None
                    or (args.format == "aac" and be_kind == "qaac"))
        if args.format == "alac":
            if use_qaac:
                say(f"Encoding ALAC with {qaac_version}")
            else:
                say("Note: qaac not found, encoding ALAC with ffmpeg")
        extra_tags = ff_extra if tmpl is not None else {}
        if not tmpl and be_kind == "opusenc":
            # opusenc tags natively (ffmpeg can't write opus pictures),
            # so it needs source tags up front like qaac always did.
            # (albumartist is stored as album_artist by ffmpeg: ask both)
            extra_tags = read_tags(input_path, "album", "genre", "date",
                                   "track", "compilation", "albumartist",
                                   "album_artist")
        # Final-encode triple for ffmpeg-backed outputs: (codec, extra
        # ffmpeg args, forced depth). Lossy+ffmpeg passes only the user's
        # --lossy-opts and skips depth forcing (encoders manage it);
        # lossless passes the format compression defaults (FLAC 12).
        if is_lossy_output and be_kind == "ffmpeg":
            enc_codec = out_codec
            enc_args = list(lossy_opts)
            enc_fmt = None
        else:
            enc_codec = out_codec
            enc_args = list(OUTPUT_FORMATS[args.format].get("ffmpeg_args")
                            or [])
            enc_fmt = depth_fmt

        meta_src = None if tmpl is not None else input_path

        def finalize_ffmpeg(wav_path, final_path, title, codec, sample_fmt,
                            ffmpeg_args=None, drop_video=False,
                            drop_tags=None):
            run_ffmpeg(wav_path, final_path, None, codec, title,
                       overwrite=args.overwrite,
                       metadata_from=meta_src,
                       sample_fmt=sample_fmt,
                       with_cover=map_cover and meta_src is not None,
                       cover_file=(flag_cover or
                                   (cover_tmp if tmpl is not None else None)),
                       strip_tags=tmpl is not None,
                       extra_tags=extra_tags or None,
                       ffmpeg_args=ffmpeg_args,
                       drop_video=drop_video,
                       drop_tags=drop_tags)
            normalize_cover(final_path)

        def normalize_cover(final_path):
            if out_ext == ".flac" and (
                    norm_image is not None or want_c is not None
                    or want_d is not None or stub_encoder is not None):
                if not flac_postprocess(final_path, norm_image, norm_mime,
                                        want_c, want_d, stub_encoder):
                    print(f"Warning: metaflac fixes failed on {final_path}",
                          file=sys.stderr)

        def finalize_qaac(wav_path, final_path, title, alac=True,
                          extra_args=()):
            if sample_is_float(wav_path):
                int_wav = temp_wav()
                run_ffmpeg(wav_path, int_wav, None, depth_pcm,
                           title, overwrite=True)
                wav_path = int_wav
            qtmp = temp_file("m4a")
            run_qaac_alac(wav_path, qtmp, alac=alac, extra_args=extra_args)
            # Tags and cover art go through ffmpeg, never qaac. AAC is
            # lossy: drop stale source encoder tags (ALAC keeps them).
            finalize_ffmpeg(qtmp, final_path, title, "copy", None,
                            drop_tags=None if alac else STALE_TAGS)

        def finalize_lame(wav_path, final_path, title):
            ltmp = temp_file("mp3")
            run_lame(wav_path, ltmp, lossy_opts)
            # Tags and cover art go through ffmpeg, never lame.
            finalize_ffmpeg(ltmp, final_path, title, "copy", None,
                            drop_tags=STALE_TAGS)

        def opus_tags_for(title):
            if tmpl is not None:
                tags = {str(k).lower(): v for k, v in ff_extra.items()
                        if str(k).lower() != "encoder"}
            else:
                tags = dict(extra_tags)
                if artist:
                    tags["artist"] = artist
            tags["title"] = title
            return tags

        def finalize_opusenc(wav_path, final_path, title):
            cover = flag_cover or cover_tmp
            run_opusenc(wav_path, final_path, opus_tags_for(title),
                        cover, lossy_opts)

        def finalize_wav(wav_path, final_path, title, codec, sample_fmt):
            drop = (is_lossy_output and be_kind == "ffmpeg"
                    and args.format == "opus")
            finalize_ffmpeg(wav_path, final_path, title, codec,
                            sample_fmt, ffmpeg_args=enc_args or None,
                            drop_video=drop,
                            drop_tags=STALE_TAGS if is_lossy_output else None)

        # Cover art: --cover wins outright; otherwise the original picture
        # is reused (extracted file for qaac/lame/opusenc, mapped stream
        # for ffmpeg). Only wav cannot carry pictures; opus pictures go
        # via opusenc --picture (ffmpeg ogg-opus rejects video).
        cover_ok_format = out_ext in (".flac", ".m4a", ".mp3", ".opus")
        flag_cover = cover_flag if (cover_flag is not None
                                    and cover_ok_format) else None
        if cover_flag is not None and not cover_ok_format:
            print(f"Warning: --cover ignored for {out_ext} output "
                  "(format cannot carry pictures)", file=sys.stderr)
        if (flag_cover is not None and out_ext == ".opus"
                and be_kind == "ffmpeg"):
            # ffmpeg's ogg-opus muxer rejects video streams outright;
            # only opusenc can attach pictures to .opus.
            print("Warning: --cover ignored for ffmpeg Opus output "
                  "(use opusenc backend for Opus pictures)", file=sys.stderr)
            flag_cover = None
        if stub_encoder is not None and out_ext != ".flac":
            print("Warning: custom 'encoder' tags only supported for FLAC "
                  "output; skipped", file=sys.stderr)
        orig_cover = None
        if flag_cover is None and cover_ok_format:
            cc = cover_codec_name(input_path)
            if cc in COVER_CODECS:
                orig_cover = cc
        # ffmpeg ogg-opus takes no pictures at all; opusenc handles them.
        map_cover = (flag_cover is None and orig_cover is not None
                     and cover_ok_format and out_ext != ".opus")
        # FLAC finals get post-processing via metaflac: Cover (front)
        # type (ffmpeg never propagates picture types), exact
        # COMMENT/DESCRIPTION (ffmpeg merges COMMENT into DESCRIPTION on
        # write), and custom ENCODER values (ffmpeg stamps its own or,
        # under bitexact, nothing). m4a covr atoms carry no type and wav
        # carries no pictures, so only flac qualifies.
        want_c = want_d = None
        if tmpl is not None:
            for key, value in ff_extra.items():
                if key.lower() == "comment":
                    want_c = value
                elif key.lower() == "description":
                    want_d = value
        elif out_ext == ".flac":
            src = read_tags(input_path, "comment", "description")
            want_c, want_d = src.get("comment"), src.get("description")
        need_meta = (out_ext == ".flac"
                     and (flag_cover is not None or orig_cover is not None
                          or want_c is not None or want_d is not None
                          or stub_encoder is not None))
        if need_meta and shutil.which("metaflac") is None:
            print("Error: metaflac not found (install flac); this FLAC "
                  "output needs Cover (front) / comment / encoder fixes",
                  file=sys.stderr)
            return 127
        norm_image, norm_mime = None, None
        if need_meta and flag_cover is not None:
            norm_image = flag_cover
            norm_mime = _IMAGE_MIME[flag_cover.suffix.lower()]

        tempdir = None
        cli_backend = be_kind in ("lame", "opusenc", "qaac")
        if (use_qaac or cli_backend
                or any(plan[0] != "direct" for _, plan in jobs)
                or (need_meta and flag_cover is None
                    and orig_cover is not None)):
            tempdir = Path(tempfile.mkdtemp(prefix="nightcore-"))
        # One-line backend label for the per-job stage headers below.
        if use_qaac:
            enc_label = "qaac"
        elif be_kind is not None:
            enc_label = be_kind
        else:
            enc_label = out_codec

        try:
            step = 0

            def temp_file(ext):
                nonlocal step
                step += 1
                assert tempdir is not None
                return tempdir / f"step{step}.{ext}"

            def temp_wav():
                return temp_file("wav")

            # Attached pictures don't survive temp WAVs: extract the original
            # once for template-mode cover replacement; the same file feeds
            # Cover (front) normalization below. Plain ffmpeg finals map
            # covers straight from the source.
            cover_tmp = None
            need_cover_file = (need_meta or cli_backend
                               or (tmpl is not None and cover_ok_format
                                   and any(plan[0] != "direct"
                                           for _, plan in jobs)))
            if (flag_cover is None and orig_cover is not None
                    and need_cover_file):
                try:
                    ext = COVER_CODECS[orig_cover]
                    cover_tmp = tempdir / f"cover.{ext}"
                    subprocess.run(
                        ["ffmpeg", "-nostdin", "-y",
                         "-i", str(input_path),
                         "-map", "0:v", "-c", "copy", str(cover_tmp)],
                        check=True, capture_output=True,
                    )
                    if norm_image is None and out_ext == ".flac":
                        norm_image = cover_tmp
                        norm_mime = ("image/jpeg" if ext == "jpg"
                                     else "image/png")
                except (subprocess.CalledProcessError, OSError):
                    cover_tmp = None

            for ji, ((suffix, plan), (_, output_path)) in enumerate(
                    zip(jobs, outputs)):
                new_title = original_title + suffix
                if tmpl is not None and not title_base:
                    new_title = new_title.lstrip()
                if args.simple_title is not None:
                    new_title = simple_title(new_title, args.simple_title)
                kind = plan[0]
                if kind == "direct":
                    desc = ("speedup" if plan[1] is not None else "convert")
                elif kind == "resample":
                    desc = f"resample to {format_rate(plan[2])}"
                else:
                    desc = f"pitch {plan[3]:+g}st"
                    if plan[4] is not None:
                        desc += f" + resample to {format_rate(plan[4])}"
                say(f"[{ji + 1}/{total}] {output_path.name}: {desc} "
                    f"-> {args.format} via {enc_label}")
                _active_outputs.add(output_path)
                if kind == "direct":
                    if use_qaac:
                        tmp = temp_wav()
                        run_ffmpeg(input_path, tmp, plan[1], depth_pcm,
                                   new_title, overwrite=True)
                        is_alac = (args.format == "alac")
                        finalize_qaac(tmp, output_path, new_title,
                                      alac=is_alac,
                                      extra_args=([] if is_alac
                                                  else lossy_opts))
                    elif be_kind == "lame":
                        tmp = temp_wav()
                        run_ffmpeg(input_path, tmp, plan[1], "pcm_f32le",
                                   new_title, overwrite=True)
                        finalize_lame(tmp, output_path, new_title)
                    elif be_kind == "opusenc":
                        tmp = temp_wav()
                        run_ffmpeg(input_path, tmp, plan[1], "pcm_f32le",
                                   new_title, overwrite=True)
                        finalize_opusenc(tmp, output_path, new_title)
                    elif is_lossy_output and be_kind == "ffmpeg":
                        run_ffmpeg(input_path, output_path, plan[1],
                                   enc_codec, new_title,
                                   overwrite=args.overwrite,
                                   sample_fmt=enc_fmt,
                                   cover_file=flag_cover,
                                   strip_tags=tmpl is not None,
                                   extra_tags=extra_tags or None,
                                   ffmpeg_args=enc_args,
                                   drop_video=(args.format == "opus"),
                                   drop_tags=STALE_TAGS)
                        normalize_cover(output_path)
                    else:
                        run_ffmpeg(input_path, output_path, plan[1], out_codec,
                                   new_title, overwrite=args.overwrite,
                                   sample_fmt=depth_fmt,
                                   cover_file=flag_cover,
                                   strip_tags=tmpl is not None,
                                   extra_tags=extra_tags or None,
                                   ffmpeg_args=enc_args or None)
                        normalize_cover(output_path)
                elif kind == "resample":
                    _, prefilter, target = plan
                    tmp1, tmp2 = temp_wav(), temp_wav()
                    run_ffmpeg(input_path, tmp1, prefilter, "pcm_f32le",
                               new_title, overwrite=True)
                    say(f"  SoX resample to {format_rate(target)}")
                    run_sox(tmp1, tmp2, ["rate", "-v", str(target)])
                    if use_qaac:
                        is_alac = (args.format == "alac")
                        finalize_qaac(tmp2, output_path, new_title,
                                      alac=is_alac,
                                      extra_args=([] if is_alac
                                                  else lossy_opts))
                    elif be_kind == "lame":
                        finalize_lame(tmp2, output_path, new_title)
                    elif be_kind == "opusenc":
                        finalize_opusenc(tmp2, output_path, new_title)
                    else:
                        finalize_wav(tmp2, output_path, new_title,
                                     enc_codec, enc_fmt)
                elif kind == "pitch":
                    _, prefilter, pitched_rate, pitch_st, target, rb_job = plan
                    tmp1 = temp_wav()
                    run_ffmpeg(input_path, tmp1, prefilter, "pcm_f32le",
                               new_title, overwrite=True)
                    # tmp1 rate: post-prefilter if any, else the input rate.
                    cur_rate = (round(sample_rate * speed)
                                if prefilter is not None else sample_rate)
                    cur = tmp1
                    if pitch_st >= 0 and pitched_rate != cur_rate:
                        # Shift up: SoX pre-scales first, otherwise shifted
                        # highs alias straight back into the audible band.
                        tmp2 = temp_wav()
                        say(f"  SoX prescale to {format_rate(pitched_rate)} "
                            "for pitch headroom")
                        run_sox(cur, tmp2, ["rate", "-v", str(pitched_rate)])
                        cur = tmp2
                        cur_rate = pitched_rate
                    tmp3 = temp_wav()
                    say(f"  rubberband pitch {pitch_st:+g}st")
                    run_rubberband(cur, tmp3, pitch_st, rb_job)
                    cur = tmp3
                    # Settle the rate afterwards: the explicit target, else
                    # the pitched rate for down-shifts (up-shifts already
                    # sit at it from the pre-scale above).
                    settle = (target if target is not None
                              else (pitched_rate if pitch_st < 0 else None))
                    if settle is not None and settle != cur_rate:
                        tmp4 = temp_wav()
                        say(f"  SoX settle to {format_rate(settle)}")
                        run_sox(cur, tmp4, ["rate", "-v", str(settle)])
                        cur = tmp4
                    if use_qaac:
                        is_alac = (args.format == "alac")
                        finalize_qaac(cur, output_path, new_title,
                                      alac=is_alac,
                                      extra_args=([] if is_alac
                                                  else lossy_opts))
                    elif be_kind == "lame":
                        finalize_lame(cur, output_path, new_title)
                    elif be_kind == "opusenc":
                        finalize_opusenc(cur, output_path, new_title)
                    else:
                        finalize_wav(cur, output_path, new_title,
                                     enc_codec, enc_fmt)
                _active_outputs.discard(output_path)
        finally:
            if tempdir is not None:
                shutil.rmtree(tempdir, ignore_errors=True)
        return 0

    except subprocess.CalledProcessError as e:
        print(f"\nffmpeg/sox/rubberband/qaac/lame/opusenc failed with exit code {e.returncode}", file=sys.stderr)
        return 1

    except FileNotFoundError:
        print("Error: a required tool went missing mid-run. ffmpeg is always needed (plus sox for resampling, rubberband for pitch).", file=sys.stderr)
        return 127



def resolve_audio_naming(args, tmpl, rb_extras, input_path, speed,
                           combo, st_signed, combo_seq, eff_semitones,
                           denight=None, quiet=False):
    """Compute everything the output filename needs, without converting.

    denight is None, or dict(d_speed, d_st, d_seq, d_rb, cleaned_stem)
    from parsing filename tags in -D mode. Returns a dict with speed,
    sample_rate, target_rate, is_lossy_output, codec bits info,
    pitch_mode, pitch_rb, weird/standard suffixes, out_ext/codec/depths,
    artist, original_title, title_base, ff_extra and stub_encoder --
    or None when the sample rate cannot be probed (caller reports it).
    Warnings (-R ignored/exceeds) print unless quiet (lyrics mirroring
    predicts names without nagging twice).
    """
    if denight is not None:
        d_speed = denight["d_speed"]
        d_st = denight["d_st"]
        d_seq = denight["d_seq"]
        d_rb = denight["d_rb"]
        speed = 1.0 / d_speed
    else:
        d_st = 0.0
        d_seq = 1.0
        d_rb = None

    sample_rate = get_sample_rate(input_path)
    if sample_rate is None:
        return None

    # Decide resample target. Default: no resampling (bit-perfect weird rate).
    # In -D mode the undo itself changes the rate, so select from that.
    # Lossy outputs always keep the weird rate bit-perfect (-R is ignored
    # with a warning): the encoder gets the untouched rate, and if it
    # chokes on it, it fails loudly instead of silently resampling.
    is_lossy_output = bool(OUTPUT_FORMATS[args.format].get("lossy"))
    target_rate = None
    weird_rate = sample_rate * speed * combo_seq / d_seq
    if is_lossy_output and args.resample:
        if not quiet:
            print("Warning: -R ignored for lossy output "
                  "(weird rate passed through bit-perfect)", file=sys.stderr)
    elif args.resample:
        target_rate = next_standard_rate(weird_rate)
        if target_rate is None:
            if not quiet:
                print(
                    f"Warning: effective rate {weird_rate:.0f} Hz exceeds "
                    f"highest known standard rate; keeping weird rate.",
                    file=sys.stderr,
                )
        elif target_rate == round(weird_rate):
            # Already a standard rate: resampling would be a no-op.
            target_rate = None

    # Build filename/title suffix:
    # "[<speed>x|X.XXst] [<codec> <bitrate>k] [<rate>]"
    # - speed/st omitted when amount is 1 (0st)
    # - lossy tag added when the source codec is lossy (to flag that the
    #   output is a lossless container around a lossy source)
    # - rate appended on resample.
    codec, bit_rate, bits, format_bit_rate, raw_encoder = get_audio_info(input_path)
    mode, encoder = None, clean_encoder(raw_encoder, codec)
    if codec == "mp3":
        mode, lame = mp3_header_info(input_path)
        encoder = lame or encoder
        if mode == "VBR" and format_bit_rate:
            # Stream bit_rate on VBR MP3s is unreliable; use file average.
            bit_rate = format_bit_rate
    elif codec == "opus":
        # Opus has true CBR and VBR modes but stores no flag for them.
        mode = packet_vbr_info(input_path)
    # Vorbis is variable by design and AAC exposes no reliable in-band
    # mode signal, so neither gets a VBR/CBR label.
    lossy_suffix = lossy_suffix_for(codec, bit_rate, mode, encoder)
    if lossy_suffix:
        # e.g. [mp3 CBR 320k LAME3.100]: documents a lossy origin.
        middle_suffix = lossy_suffix
    elif bits:
        # e.g. [24bit 44.1kHz]: documents lossless source depth/rate,
        # which is what you need to revert to original speed.
        middle_suffix = f" [{bits}bit {format_rate(sample_rate)}]"
    else:
        # Float/unknown depth (e.g. 32-bit float masters): rate only.
        middle_suffix = f" [{format_rate(sample_rate)}]"
    # -S/--semitones implies pitch-only; -p is the -s spelling of the same.
    # -D inverts parsed tags: pitch is needed whenever parsed st != 0.
    pitch_mode = ((args.pitch or eff_semitones is not None) and speed != 1.0
                  or (args.denightcore and d_st != 0.0))
    if pitch_mode:
        # In pitch mode the label is semitones; --semitones input is reused
        # verbatim, otherwise derived from the speed factor. Combo jobs
        # label both parts: [1.2x 2.166866948st].
        st_label = (st_signed if st_signed is not None
                    else semitones_from_speed(speed))
        # Full precision: negating the label value inverts the shift exactly.
        if combo:
            mode_suffix = f" [{speed:g}x {st_label!r}st]"
        else:
            mode_suffix = f" [{st_label!r}st]"
    else:
        mode_suffix = "" if speed == 1.0 else f" [{speed:g}x]"
    rate_suffix = f" [{format_rate(target_rate)}]" if target_rate is not None else ""
    # Recorded rubberband extras ride along in their own tag so -D can
    # reapply them: command-line extras, else the ones parsed from the
    # filename (already stripped of any -p, whose value lives in [Nst]).
    # Extras are only recorded when a pitch job actually runs; otherwise
    # they had no effect and must not pollute the filename.
    if args.denightcore:
        if args.rb_params:
            pitch_rb = list(rb_extras) if pitch_mode else []
        elif d_rb:
            try:
                pitch_rb, _ = parse_rb_params(d_rb)
            except ValueError:
                pitch_rb = []
            if not pitch_mode:
                pitch_rb = []
        else:
            pitch_rb = []
    else:
        pitch_rb = list(rb_extras) if pitch_mode else []
    rb_suffix = (" [rb %s]" % " ".join(pitch_rb)) if pitch_rb else ""
    if is_lossy_output:
        # Lossy sharing files carry only the shift amount: no source-quality
        # tag (stale or misleading once re-encoded) and no rate tag (the
        # rate is always a compatible standard one by construction).
        middle_suffix = ""
        rate_suffix = ""
    weird_suffix = mode_suffix + rb_suffix + middle_suffix
    standard_suffix = weird_suffix + rate_suffix
    if args.denightcore:
        # Output name comes from the stripped input name instead; only a
        # fresh rate tag is appended when resampling now.
        cleaned_stem = denight["cleaned_stem"]
        weird_suffix, standard_suffix = "", rate_suffix
    else:
        cleaned_stem = None

    out_ext = OUTPUT_FORMATS[args.format]["ext"]
    out_codec = OUTPUT_FORMATS[args.format]["codec"] or wav_codec_for(bits)
    # Collapse sub-16-bit/unknown (lossy, float) sources to 16-bit output:
    # an MP3 carries no more real detail, and float temps would otherwise
    # bloat finals to 32-bit. Genuine 24/32-bit sources keep their depth.
    depth_fmt = None if (bits is not None and bits > 16) else "s16"
    depth_pcm = wav_codec_for(bits)

    stem = input_path.stem
    original_title = get_title(input_path, fallback=stem)
    artist = get_artist(input_path)

    # Tag template: override/keep source tags, strip everything else at
    # encode time. Title still gets the computed suffix afterwards.
    ff_extra = {}
    title_base = original_title
    stub_encoder = None
    if tmpl is not None:
        entry = tmpl.get("title")
        if entry is None:
            title_base = ""
        elif entry[1] is not None:
            title_base = entry[1]
        entry = tmpl.get("artist")
        if entry is None:
            artist = None
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

    if is_lossy_output:
        # Lossy sharing files carry no source-quality or rate tags: strip
        # them from the inherited title/filename base as well (an old
        # "[16bit 44.1kHz]" riding along in the source title must not
        # survive the transcode). Shift tags stay: only quality/rate go.
        original_title = strip_quality_rate_tags(original_title)
        if args.denightcore:
            cleaned_stem = strip_quality_rate_tags(cleaned_stem)

    return {
        "speed": speed,
        "sample_rate": sample_rate,
        "target_rate": target_rate,
        "is_lossy_output": is_lossy_output,
        "bits": bits,
        "pitch_mode": pitch_mode,
        "pitch_rb": pitch_rb,
        "weird_suffix": weird_suffix,
        "standard_suffix": standard_suffix,
        "cleaned_stem": cleaned_stem,
        "out_ext": out_ext,
        "out_codec": out_codec,
        "depth_fmt": depth_fmt,
        "depth_pcm": depth_pcm,
        "artist": artist,
        "original_title": original_title,
        "title_base": title_base,
        "ff_extra": ff_extra,
        "stub_encoder": stub_encoder,
    }


def predict_audio_stem(args, ctx, audio_path):
    """Predict the output filename stem run_conversion would use.

    Used by .lrc lyrics jobs so they follow their parent audio file
    with the same name. Returns the sanitized stem (no extension), or
    None when it cannot be determined (unreadable file, unknown rate).
    Picks the resampled variant under --both, like the compatible file
    a player would actually pair with. Never prints, never raises.
    """
    if audio_path is None:
        return None
    try:
        naming = resolve_audio_naming(
            args, ctx["tmpl"], ctx["rb_extras"], Path(audio_path),
            ctx["speed"], ctx["combo"], ctx["st_signed"],
            ctx["combo_seq"], ctx["eff_semitones"],
            denight=None, quiet=True)
    except (OSError, ValueError):
        return None
    if naming is None:
        return None
    suffix = (naming["standard_suffix"]
              if naming["target_rate"] is not None
              else naming["weird_suffix"])
    if args.denightcore:
        name_base = naming["cleaned_stem"]
    else:
        artist_prefix = f"{naming['artist']} - " if naming["artist"] else ""
        name_base = artist_prefix + naming["original_title"]
    return sanitize_filename(name_base + suffix)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Second Ctrl+C (or an interrupt outside the tracked tools):
        # partial outputs go, tempdirs already cleaned themselves.
        for partial in _active_outputs:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
