"""Probing (ffprobe), format math, and cover extraction."""

import json
import math
import re
import subprocess
import sys

from .constants import COVER_CODECS, STANDARD_SAMPLE_RATES
from .native_tags import native_format_tags

_probe_cache = {}


_probe_no_ffprobe = False


def probe_audio(path):
    """Everything ffprobe knows about a file, in one pass (cached).

    Returns {"audio": {...}, "video": [...], "format": {...},
    "format_tags": {...}} with missing sections as {} / []. Results are
    cached by path for the run: inputs are never rewritten mid-run
    (conversion outputs land elsewhere, -N probes before replacing,
    temps have unique paths), so one probe per file is enough. The
    packet-level VBR scan and sox stats keep their own probes: those
    read frames/samples, not stream metadata. Never raises.
    """
    key = str(path)
    if key not in _probe_cache:
        _probe_cache[key] = _probe_audio_uncached(path)
    return _probe_cache[key]


def _probe_audio_uncached(path):
    empty = {"audio": {}, "video": [], "format": {}, "format_tags": {}}
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries",
             ("stream=codec_type,codec_name,bit_rate,sample_fmt,"
              "bits_per_raw_sample,sample_rate"),
             "-show_entries", "stream_tags",
             "-show_entries", "format=bit_rate",
             "-show_entries", "format_tags",
             "-of", "json",
             str(path)],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        print("ffprobe failed:", (e.stderr or "").strip(), file=sys.stderr)
        return empty
    except (FileNotFoundError, OSError):
        global _probe_no_ffprobe
        _probe_no_ffprobe = True
        return empty
    try:
        info = json.loads(result.stdout or "{}")
    except ValueError:
        return empty
    streams = [s for s in (info.get("streams") or [])
               if isinstance(s, dict)]
    fmt = info.get("format")
    if not isinstance(fmt, dict):
        fmt = {}
    return {
        "audio": next((s for s in streams
                       if str(s.get("codec_type") or "").lower() == "audio"),
                      {}),
        "video": [s for s in streams
                  if str(s.get("codec_type") or "").lower() == "video"],
        "format": fmt,
        "format_tags": fmt.get("tags") or {},
    }


def get_sample_rate(input_path):
    raw = probe_audio(input_path)["audio"].get("sample_rate")
    if raw is None:
        if _probe_no_ffprobe:
            print("Error: ffprobe not found. Is ffmpeg installed?",
                  file=sys.stderr)
        return None
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError:
        print(
            f"Error: unexpected sample_rate from ffprobe: {text!r}",
            file=sys.stderr,
        )
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
    """Depth-matched PCM encoder (temp WAVs, depth decisions)."""
    if bits is None:
        return "pcm_s16le"  # e.g. lossy source decoded to float
    if bits <= 16:
        return "pcm_s16le"
    if bits <= 24:
        return "pcm_s24le"
    return "pcm_s32le"


def get_audio_info(input_path):
    """(codec, bit_rate, bits, format_bit_rate, raw_encoder) for audio.

    codec is lowercased or None; bit_rate is int or None (falls back to
    the format bit_rate when the stream bit_rate is missing); bits is
    int or None (from bits_per_raw_sample, else mapped from sample_fmt;
    None for float/unknown, e.g. lossy decodes).
    """
    probe = probe_audio(input_path)
    stream = probe["audio"]
    fmt = probe["format"]
    fmt_tags = probe["format_tags"]
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
    raw_encoder = stream_tags.get("encoder") or fmt_tags.get("encoder")
    if raw_encoder is None:
        # Vorbis-family encoder fields are invisible to ffprobe (see
        # native_tags); consult the native reader before giving up.
        own = native_format_tags(input_path)
        if own:
            for k, v in own.items():
                if k.lower() == "encoder" and str(v).strip():
                    raw_encoder = str(v).strip()
                    break

    return codec, bit_rate, bits_from_stream(stream), format_bit_rate, raw_encoder


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


def sample_is_float(path):
    """True if audio decodes as float (or the probe fails: convert anyway)."""
    fmt = str(probe_audio(path)["audio"].get("sample_fmt") or "")
    fmt = fmt.strip().lower()
    return not fmt or fmt.startswith(("flt", "dbl"))


def cover_codec_name(path):
    """Video codec of the first attached picture stream, or None."""
    for stream in probe_audio(path)["video"]:
        name = str(stream.get("codec_name") or "").strip().lower()
        if name:
            return name
    return None


def extract_cover_tmp(input_path, orig_cover, tempdir):
    """Copy the attached picture to a temp image file; None on failure."""
    try:
        cover_tmp = tempdir / f"cover.{COVER_CODECS[orig_cover]}"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y",
             "-i", str(input_path),
             "-map", "0:v", "-c", "copy", str(cover_tmp)],
            check=True, capture_output=True,
        )
        return cover_tmp
    except (subprocess.CalledProcessError, OSError):
        return None
