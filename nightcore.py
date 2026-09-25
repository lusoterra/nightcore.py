#!/usr/bin/env python3

import argparse
import json
import math
import re
import shutil
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

# Lossless output formats: extension + ffmpeg audio codec.
# All of these round-trip the Title tag and accept weird asetrate rates.
# "wav" uses a depth-matched PCM codec (see wav_codec_for()).
OUTPUT_FORMATS = {
    "flac": {"ext": ".flac", "codec": "flac"},
    "alac": {"ext": ".m4a", "codec": "alac"},
    "wav": {"ext": ".wav", "codec": None},
}


def _help_formatter(*args, **kwargs):
    kwargs.setdefault("max_help_position", 32)
    return argparse.RawTextHelpFormatter(*args, **kwargs)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Change audio speed/pitch using ffmpeg and sox",
        formatter_class=_help_formatter
    )

    parser.add_argument(
        "input",
        type=str,
        nargs="?",
        default=None,
        help="Input audio file (not needed for --pitch-diff/--speed-diff)"
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
        help="Output format: flac (default), alac (.m4a), wav"
    )

    parser.add_argument(
        "-p", "--pitch",
        action="store_true",
        default=False,
        help=(
            "Pitch-shift only, keep duration (needs sox)\n"
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

    return parser.parse_args()


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
    """Strip shift/rate [...] groups; return (cleaned, speed, st, found).

    Removes bracket groups made only of shift tokens ([1.2x], [3.15st],
    combined [1.2x 2.16st]) and bare rate tags ([88.2kHz]). Quality tags
    ([mp3 CBR 320k LAME], [16bit 44.1kHz]) and anything else stay verbatim.
    speed = product of [Nx], st = sum of [Nst]; found tells whether any
    shift token was seen at all.
    """
    speed, st, found = 1.0, 0.0, False

    def _strip(m):
        nonlocal speed, st, found
        toks = m.group(1).split()
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

    return " ".join(re.sub(r"\[([^\[\]]*)\]", _strip, name).split()), speed, st, found


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
    subprocess.run(
        ["sox", "-S", str(input_path), str(output_path)] + effects,
        check=True,
    )


def missing_tools(*names):
    """Names of required executables not found on PATH."""
    return [n for n in names if shutil.which(n) is None]


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


def run_qaac_alac(input_wav, output_m4a, tags, overwrite, artwork=None):
    """Encode ALAC with qaac (CoreAudio); raises CalledProcessError on failure.

    input_wav must be integer PCM (qaac rejects float). tags maps
    title/artist/album/genre/date/track; missing/empty values are skipped.
    artwork: optional cover-image file passed via --artwork.
    Our own -n/-y preflight already ran, and qaac overwrites silently.
    """
    del overwrite  # handled by our preflight; qaac overwrites regardless
    cmd = ["qaac", "--silent", "--alac", "--rate", "keep"]
    for flag, key in (("--title", "title"), ("--artist", "artist"),
                      ("--album", "album"), ("--genre", "genre"),
                      ("--date", "date")):
        if tags.get(key):
            cmd += [flag, tags[key]]
    track = tags.get("track")
    if track and re.fullmatch(r"\d+(/\d+)?", track):
        cmd += ["--track", track]
    if artwork is not None:
        cmd += ["--artwork", str(artwork)]
    cmd += ["-o", str(output_m4a), str(input_wav)]
    subprocess.run(cmd, check=True)


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


def run_ffmpeg(input_path, output_path, audio_filter, codec, title, overwrite,
               metadata_from=None, sample_fmt=None, with_cover=False):
    """Run one ffmpeg transcode; raises CalledProcessError on failure.

    metadata_from: optional extra input whose global metadata is mapped
    to the output (used to restore tags lost by intermediate sox steps);
    the explicit title always wins. Audio is always taken from input_path.
    sample_fmt: optional forced output depth (s16/s24/s32); None leaves
    the encoder's default negotiation (used for lossless temps/sources).
    with_cover: also map the attached picture + stream tags from
    metadata_from (ffmpeg flac/m4a finals on the sox path).
    """
    ffmpeg_cmd = [
        "ffmpeg",
        "-nostdin",
        "-y" if overwrite else "-n",
        "-i", str(input_path),
    ]
    if metadata_from is not None:
        ffmpeg_cmd += ["-i", str(metadata_from), "-map", "0:a", "-map_metadata", "1",
                       "-map_metadata:s:a", "1:s:a"]
        if with_cover:
            ffmpeg_cmd += ["-map", "1:v", "-c:v", "copy",
                           "-disposition:v", "attached_pic"]
    if audio_filter is not None:
        ffmpeg_cmd += ["-af", audio_filter]
    if sample_fmt is not None:
        ffmpeg_cmd += ["-sample_fmt", sample_fmt]
    ffmpeg_cmd += [
        "-c:a", codec,
        "-metadata", f"title={title}",
        str(output_path)
    ]
    subprocess.run(ffmpeg_cmd, check=True)


def main():
    args = parse_arguments()

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

    if not args.input:
        print("Error: input file is required", file=sys.stderr)
        return 1

    input_path = Path(args.input)

    if not input_path.is_file():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
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
    # SoX upsamples by the semitone factor and pitch-shifts.
    combo = args.semitones is not None and args.speed != 1.0
    # st_signed is the semitone amount actually applied (reciprocal negates).
    st_signed = (-args.semitones if args.reciprocal else args.semitones
                 if args.semitones is not None else None)
    combo_seq = speed_from_semitones(st_signed) if combo else 1.0
    if args.semitones is not None and not combo:
        speed = speed_from_semitones(args.semitones)
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

    # -D reads the shift back out of the filename tags ([1.2x], [3.15st],
    # combined [1.2x 2.16st]) instead of taking -s/-S/-p/-r.
    d_speed, d_st, d_seq = 1.0, 0.0, 1.0
    cleaned_stem = None
    if args.denightcore:
        for flag, bad in (("-s/--speed", args.speed != 1.0),
                          ("-S/--semitones", args.semitones is not None),
                          ("-p/--pitch", args.pitch),
                          ("-r/--reciprocal", args.reciprocal)):
            if bad:
                print(f"Error: --denightcore cannot be combined with {flag} "
                      "(it reads the shift from filename tags)", file=sys.stderr)
                return 1
        cleaned_stem, d_speed, d_st, found = split_shift_tags(input_path.stem)
        if not found:
            print(f"Error: no [Nx] or [Nst] shift tags in '{input_path.name}'; "
                  "--denightcore reads the amount from filename tags like "
                  "[1.2x] or [3.15st], e.g. nightcore 'Song [1.2x].flac' -D",
                  file=sys.stderr)
            return 1
        speed = 1.0 / d_speed
        d_seq = speed_from_semitones(d_st)

    sample_rate = get_sample_rate(input_path)
    if sample_rate is None:
        print("Error: could not determine input sample rate", file=sys.stderr)
        return 1

    # Decide resample target. Default: no resampling (bit-perfect weird rate).
    # In -D mode the undo itself changes the rate, so select from that.
    target_rate = None
    if args.resample:
        effective_rate = sample_rate * speed * combo_seq / d_seq
        target_rate = next_standard_rate(effective_rate)
        if target_rate is None:
            print(
                f"Warning: effective rate {effective_rate:.0f} Hz exceeds "
                f"highest known standard rate; keeping weird rate.",
                file=sys.stderr,
            )
        elif target_rate == round(effective_rate):
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
    pitch_mode = ((args.pitch or args.semitones is not None) and speed != 1.0
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
    weird_suffix = mode_suffix + middle_suffix
    standard_suffix = weird_suffix + rate_suffix
    if args.denightcore:
        # Output name comes from the stripped input name instead; only a
        # fresh rate tag is appended when resampling now.
        weird_suffix, standard_suffix = "", rate_suffix

    out_ext = OUTPUT_FORMATS[args.format]["ext"]
    out_codec = OUTPUT_FORMATS[args.format]["codec"] or wav_codec_for(bits)
    # Collapse sub-16-bit/unknown (lossy, float) sources to 16-bit output:
    # an MP3 carries no more real detail, and float temps would otherwise
    # bloat finals to 32-bit. Genuine 24/32-bit sources keep their depth.
    depth_fmt = None if (bits is not None and bits > 16) else "s16"
    depth_pcm = wav_codec_for(bits)

    if (not args.denightcore and not weird_suffix and not rate_suffix
            and input_path.suffix.lower() == out_ext):
        print(
            "Error: nothing to do (speed is 1 and no resampling requested)",
            file=sys.stderr,
        )
        return 1
    # e.g. wav -> flac with speed 1: still convert to target format.

    if args.both and target_rate is None:
        # Requested both outputs but resampling is a no-op (already a
        # standard rate, or out of range): there is only one distinct file.
        print(
            "Warning: --both requested but resampling is a no-op; "
            "generating only the weird-rate file.",
            file=sys.stderr,
        )

    stem = input_path.stem
    original_title = get_title(input_path, fallback=stem)
    artist = get_artist(input_path)

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
    #   ("pitch", prefilter, pitched_rate, cents, target) - ffmpeg -> sox rate+pitch -> ffmpeg
    # Temp WAVs are f32 (exact for <=24-bit int and float32 sources).
    jobs: list[tuple[str, Any]]
    if pitch_mode:
        # d_seq folds a -D pitch inversion into the same math (1.0 otherwise).
        # -D on a standard-rate file inverts a rate-preserving pitch job,
        # so keep the rate instead of scaling it back down.
        if args.denightcore and d_st != 0.0 and sample_rate in STANDARD_SAMPLE_RATES:
            pitched_rate = round(sample_rate * speed)
        else:
            pitched_rate = round(sample_rate * speed * combo_seq / d_seq)
        if combo:
            cents = st_signed * 100.0
        else:
            cents = (-d_st * 100.0 if args.denightcore and d_st != 0.0
                     else semitones_from_speed(speed / d_seq) * 100.0)
        # -D combined undo slows down first via asetrate; combo speeds up
        # first the same way (plain pitch jobs decode unfiltered).
        prefilter = weird_filter() if (args.denightcore or combo) else None
        if args.both and target_rate is not None:
            jobs = [
                (weird_suffix, ("pitch", prefilter, pitched_rate, cents, None)),
                (standard_suffix, ("pitch", prefilter, pitched_rate, cents, target_rate)),
            ]
        elif target_rate is not None:
            jobs = [(standard_suffix, ("pitch", prefilter, pitched_rate, cents, target_rate))]
        else:
            jobs = [(weird_suffix, ("pitch", prefilter, pitched_rate, cents, None))]
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

    if (not args.dry_run
            and any(plan[0] != "direct" for _, plan in jobs)
            and shutil.which("sox") is None):
        print(
            "Error: 'sox' not found. --pitch and resampling (-R)\n"
            "require SoX installed (apt/pacman/brew install sox).",
            file=sys.stderr,
        )
        return 127

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
            input_path.parent / (sanitize_filename(name_base + suffix) + out_ext),
        )
        for suffix, _ in jobs
    ]

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
            print(
                f"Error: output already exists: {output_path} (use -y to overwrite)",
                file=sys.stderr,
            )
            return 1

    try:
        total = len(jobs)
        # qaac (CoreAudio, often run via wine) encodes ALAC when present;
        # otherwise ffmpeg's alac encoder is used. qaac needs integer PCM.
        qaac_version = find_qaac() if args.format == "alac" else None
        use_qaac = qaac_version is not None
        if args.format == "alac":
            if use_qaac:
                print(f"Encoding ALAC with {qaac_version}", file=sys.stderr)
            else:
                print("Note: qaac not found, encoding ALAC with ffmpeg",
                      file=sys.stderr)
        int_temp_codec = depth_pcm
        extra_tags = read_tags(input_path, "album", "genre", "date", "track") \
            if use_qaac else {}

        def qtags(title):
            tags = {"title": title}
            if artist:
                tags["artist"] = artist
            tags.update(extra_tags)
            return tags

        def finalize_qaac(wav_path, final_path, title):
            if sample_is_float(wav_path):
                int_wav = temp_wav()
                run_ffmpeg(wav_path, int_wav, None, depth_pcm,
                           title, overwrite=True)
                wav_path = int_wav
            run_qaac_alac(wav_path, final_path, qtags(title),
                          overwrite=args.overwrite, artwork=cover_tmp)

        tempdir = None
        if use_qaac or any(plan[0] != "direct" for _, plan in jobs):
            tempdir = Path(tempfile.mkdtemp(prefix="nightcore-"))
        try:
            step = 0

            def temp_wav():
                nonlocal step
                step += 1
                assert tempdir is not None
                return tempdir / f"step{step}.wav"

            # Attached pictures don't survive temp WAVs: extract once so
            # sox-path and qaac finals can reattach them. (The direct
            # ffmpeg path auto-maps covers already; the wav muxer can't
            # carry video, so wav outputs skip this.)
            cover_tmp = None
            if use_qaac or (out_ext in (".flac", ".m4a")
                            and any(plan[0] != "direct" for _, plan in jobs)):
                cc = cover_codec_name(input_path)
                if cc in COVER_CODECS:
                    try:
                        cover_tmp = tempdir / f"cover.{COVER_CODECS[cc]}"
                        subprocess.run(
                            ["ffmpeg", "-nostdin", "-y",
                             "-i", str(input_path),
                             "-map", "0:v", "-c", "copy", str(cover_tmp)],
                            check=True, capture_output=True,
                        )
                    except (subprocess.CalledProcessError, OSError):
                        cover_tmp = None

            for (suffix, plan), (_, output_path) in zip(jobs, outputs):
                new_title = original_title + suffix
                if total > 1:
                    print(f"Writing {output_path}", file=sys.stderr)
                kind = plan[0]
                if kind == "direct":
                    if use_qaac:
                        tmp = temp_wav()
                        run_ffmpeg(input_path, tmp, plan[1], int_temp_codec,
                                   new_title, overwrite=True)
                        run_qaac_alac(tmp, output_path, qtags(new_title),
                                      overwrite=args.overwrite, artwork=cover_tmp)
                    else:
                        run_ffmpeg(input_path, output_path, plan[1], out_codec,
                                   new_title, overwrite=args.overwrite,
                                   sample_fmt=depth_fmt)
                elif kind == "resample":
                    _, prefilter, target = plan
                    tmp1, tmp2 = temp_wav(), temp_wav()
                    run_ffmpeg(input_path, tmp1, prefilter, "pcm_f32le",
                               new_title, overwrite=True)
                    run_sox(tmp1, tmp2, ["rate", "-v", str(target)])
                    if use_qaac:
                        finalize_qaac(tmp2, output_path, new_title)
                    else:
                        run_ffmpeg(tmp2, output_path, None, out_codec,
                                   new_title, overwrite=args.overwrite,
                                   metadata_from=input_path,
                                   sample_fmt=depth_fmt,
                                   with_cover=cover_tmp is not None)
                elif kind == "pitch":
                    _, prefilter, pitched_rate, cents, target = plan
                    tmp1, tmp2 = temp_wav(), temp_wav()
                    run_ffmpeg(input_path, tmp1, prefilter, "pcm_f32le",
                               new_title, overwrite=True)
                    tmp_rate = (sample_rate * speed if prefilter is not None
                                else sample_rate)
                    effects = []
                    if pitched_rate != round(tmp_rate):
                        effects += ["rate", "-v", str(pitched_rate)]
                    effects += ["pitch", repr(cents)]
                    if target is not None:
                        effects += ["rate", "-v", str(target)]
                    run_sox(tmp1, tmp2, effects)
                    if use_qaac:
                        finalize_qaac(tmp2, output_path, new_title)
                    else:
                        run_ffmpeg(tmp2, output_path, None, out_codec,
                                   new_title, overwrite=args.overwrite,
                                   metadata_from=input_path,
                                   sample_fmt=depth_fmt,
                                   with_cover=cover_tmp is not None)
        finally:
            if tempdir is not None:
                shutil.rmtree(tempdir, ignore_errors=True)
        return 0

    except subprocess.CalledProcessError as e:
        print(f"\nffmpeg/sox/qaac failed with exit code {e.returncode}", file=sys.stderr)
        return 1

    except FileNotFoundError:
        print("Error: 'ffmpeg'/'sox'/'qaac' not found. Is ffmpeg installed (plus sox for --pitch/resampling)?", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
