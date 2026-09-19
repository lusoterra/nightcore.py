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


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Change audio speed/pitch using ffmpeg and sox",
        formatter_class=argparse.RawTextHelpFormatter
    )

    parser.add_argument(
        "input",
        type=str,
        nargs="?",
        default=None,
        help="Input audio file (not needed with --pitch-diff)"
    )

    parser.add_argument(
        "-s", "--speed",
        type=float,
        default=1.0,
        help="Speed (e.g. 1.2 = 20%% faster, 0.8 = 20%% slower)"
    )

    parser.add_argument(
        "-r", "--reciprocal",
        action="store_true",
        default=False,
        help="Reciprocal (e.g. 1.2 becomes 1/1.2 as the multiplier)"
    )

    parser.add_argument(
        "-y", "--overwrite",
        action="store_true",
        default=False,
        help="Overwrite output file if it exists (default: fail if exists)"
    )

    parser.add_argument(
        "-R", "--resample",
        action="store_true",
        default=False,
        help=(
            "Resample output to the next higher standard sample rate\n"
            "for player compatibility (default: keep the non-standard\n"
            "sample rate, bit-perfect for archival)."
        )
    )

    parser.add_argument(
        "--target-rate",
        type=int,
        default=None,
        metavar="HZ",
        help=(
            "Explicit resample target in Hz (implies resampling).\n"
            "Overrides --resample auto-selection."
        )
    )

    parser.add_argument(
        "-B", "--both",
        action="store_true",
        default=False,
        help=(
            "With -R/--resample or --target-rate, generate both files:\n"
            "the non-standard-rate file and the standard-rate file."
        )
    )

    parser.add_argument(
        "-F", "--format",
        choices=sorted(OUTPUT_FORMATS),
        default="flac",
        metavar="FMT",
        help=(
            "Lossless output format (default: flac).\n"
            "alac = Apple Lossless (.m4a),\n"
            "wav = Windows-friendly PCM (.wav, bit depth matched)."
        )
    )

    parser.add_argument(
        "-p", "--pitch",
        action="store_true",
        default=False,
        help=(
            "Pitch-shift only (duration preserved) instead of speeding up.\n"
            "e.g. -s 1.1 shifts pitch by 1.65st (needs sox).\n"
            "Labelled [1.65st], not [1.1x]."
        )
    )

    parser.add_argument(
        "-S", "--semitones",
        type=float,
        default=None,
        metavar="ST",
        help=(
            "Shift amount in semitones instead of -s/--speed\n"
            "(speed = 2^(ST/12); e.g. 3.16 ~= 1.2x)."
        )
    )

    parser.add_argument(
        "--pitch-diff",
        type=float,
        nargs=2,
        default=None,
        metavar=("F1", "F2"),
        help=(
            "Print the pitch difference between two peak frequencies\n"
            "(F1 = original, F2 = pitched: semitones, cents, speed) and exit."
        )
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
        kbps = int(round(bit_rate / 1000))
        if kbps > 0:
            parts.append(f"{kbps}k")
    if encoder:
        parts.append(encoder)
    return " [" + " ".join(parts) + "]"


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


def run_ffmpeg(input_path, output_path, audio_filter, codec, title, overwrite,
               metadata_from=None):
    """Run one ffmpeg transcode; raises CalledProcessError on failure.

    metadata_from: optional extra input whose global metadata is mapped
    to the output (used to restore tags lost by intermediate sox steps);
    the explicit title always wins. Audio is always taken from input_path.
    """
    ffmpeg_cmd = [
        "ffmpeg",
        "-nostdin",
        "-y" if overwrite else "-n",
        "-i", str(input_path),
    ]
    if metadata_from is not None:
        ffmpeg_cmd += ["-i", str(metadata_from), "-map", "0:a", "-map_metadata", "1"]
    if audio_filter is not None:
        ffmpeg_cmd += ["-af", audio_filter]
    ffmpeg_cmd += [
        "-c:a", codec,
        "-metadata", f"title={title}",
        str(output_path)
    ]
    subprocess.run(ffmpeg_cmd, check=True)


def main():
    args = parse_arguments()

    if args.pitch_diff is not None:
        f1, f2 = args.pitch_diff
        if f1 <= 0 or f2 <= 0:
            print("Error: --pitch-diff frequencies must be > 0", file=sys.stderr)
            return 1
        st = 12.0 * math.log2(f2 / f1)
        print(f"{f2:g} Hz vs {f1:g} Hz: {st:.2f}st ({st * 100:.2f} cents), speed {f2 / f1:.4g}x")
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

    if args.semitones is not None and args.speed != 1.0:
        print("Error: --semitones cannot be combined with -s/--speed", file=sys.stderr)
        return 1

    if args.semitones is not None:
        speed = speed_from_semitones(args.semitones)
    else:
        speed = args.speed

    if speed <= 0:
        print("Error: Speed factor must be > 0", file=sys.stderr)
        return 1

    if args.reciprocal:
        speed = 1 / speed

    if args.target_rate is not None and args.target_rate <= 0:
        print("Error: --target-rate must be > 0", file=sys.stderr)
        return 1

    if args.both and args.target_rate is None and not args.resample:
        print("Error: --both requires -R/--resample or --target-rate", file=sys.stderr)
        return 1

    sample_rate = get_sample_rate(input_path)
    if sample_rate is None:
        print("Error: could not determine input sample rate", file=sys.stderr)
        return 1

    # Decide resample target. Default: no resampling (bit-perfect weird rate).
    target_rate = None
    if args.target_rate is not None:
        target_rate = args.target_rate
    elif args.resample:
        effective_rate = sample_rate * speed
        target_rate = next_standard_rate(effective_rate)
        if target_rate is None:
            print(
                f"Warning: effective rate {effective_rate:.0f} Hz exceeds "
                f"highest known standard rate; keeping weird rate.",
                file=sys.stderr,
            )
        elif target_rate == int(round(effective_rate)):
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
    if args.pitch and speed != 1.0:
        # In pitch mode the label is semitones: -s 1.2 -> 3.16st.
        # With --semitones the input value is reused (negated by -r).
        if args.semitones is not None:
            st_label = -args.semitones if args.reciprocal else args.semitones
        else:
            st_label = semitones_from_speed(speed)
        mode_suffix = f" [{st_label:.2f}st]"
    else:
        mode_suffix = "" if speed == 1.0 else f" [{speed:g}x]"
    rate_suffix = f" [{format_rate(target_rate)}]" if target_rate is not None else ""
    weird_suffix = mode_suffix + lossy_suffix
    standard_suffix = weird_suffix + rate_suffix

    out_ext = OUTPUT_FORMATS[args.format]["ext"]
    out_codec = OUTPUT_FORMATS[args.format]["codec"] or wav_codec_for(bits)

    if not weird_suffix and not rate_suffix:
        if input_path.suffix.lower() == out_ext:
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

    def build_filter(target):
        if target is not None:
            if speed == 1.0:
                return f"aresample={target}"
            return f"asetrate={sample_rate}*{speed},aresample={target}"
        if speed == 1.0:
            # Pure container/codec conversion (e.g. mp3 -> flac): no filter.
            return None
        return f"asetrate={sample_rate}*{speed}"

    # Job list: (suffix, plan). --both emits the weird-rate file first,
    # then the standard-rate file. Plans:
    #   ("direct", audio_filter)               - ffmpeg only (weird speedup)
    #   ("resample", prefilter, target)        - ffmpeg -> sox rate -> ffmpeg
    #   ("pitch", pitched_rate, cents, target) - ffmpeg -> sox rate+pitch -> ffmpeg
    # Temp WAVs are f32 (exact for <=24-bit int and float32 sources).
    if args.pitch and speed != 1.0:
        pitched_rate = int(round(sample_rate * speed))
        cents = semitones_from_speed(speed) * 100.0
        if args.both and target_rate is not None:
            jobs = [
                (weird_suffix, ("pitch", pitched_rate, cents, None)),
                (standard_suffix, ("pitch", pitched_rate, cents, target_rate)),
            ]
        elif target_rate is not None:
            jobs = [(standard_suffix, ("pitch", pitched_rate, cents, target_rate))]
        else:
            jobs = [(weird_suffix, ("pitch", pitched_rate, cents, None))]
    elif args.both and target_rate is not None:
        jobs = [
            (weird_suffix, ("direct", build_filter(None))),
            # Prefilter is speedup-only: SoX alone performs the resampling.
            (standard_suffix, ("resample", build_filter(None), target_rate)),
        ]
    elif target_rate is not None:
        jobs = [(standard_suffix, ("resample", build_filter(None), target_rate))]
    else:
        jobs = [(weird_suffix, ("direct", build_filter(None)))]

    if any(plan[0] != "direct" for _, plan in jobs) and shutil.which("sox") is None:
        print(
            "Error: 'sox' not found. --pitch and resampling (-R/--target-rate)\n"
            "require SoX installed (apt/pacman/brew install sox).",
            file=sys.stderr,
        )
        return 127

    # Output filenames: "Artist - Title [speed|st] [<codec> <bitrate>k]
    # [<rate>]<ext>" (artist prefix omitted when the source has no artist
    # tag; "Title" is the embedded title, falling back to the filename).
    # The Title tag is the same minus the artist prefix. Names are
    # sanitized for Linux/Windows-safe filenames. Preserve input directory.
    artist_prefix = f"{artist} - " if artist else ""
    outputs = [
        (
            suffix,
            input_path.parent / (sanitize_filename(artist_prefix + original_title + suffix) + out_ext),
        )
        for suffix, _ in jobs
    ]

    for suffix, output_path in outputs:
        if output_path == input_path:
            print("Error: output would overwrite input, aborting", file=sys.stderr)
            return 1

    for suffix, output_path in outputs:
        if output_path.exists() and not args.overwrite:
            print(
                f"Error: output already exists: {output_path} (use -y to overwrite)",
                file=sys.stderr,
            )
            return 1

    try:
        total = len(jobs)
        tempdir = None
        if any(plan[0] != "direct" for _, plan in jobs):
            tempdir = Path(tempfile.mkdtemp(prefix="nightcore-"))
        try:
            step = 0

            def temp_wav():
                nonlocal step
                step += 1
                return tempdir / f"step{step}.wav"

            for (suffix, plan), (_, output_path) in zip(jobs, outputs):
                new_title = original_title + suffix
                if total > 1:
                    print(f"Writing {output_path}", file=sys.stderr)
                kind = plan[0]
                if kind == "direct":
                    run_ffmpeg(input_path, output_path, plan[1], out_codec,
                               new_title, overwrite=args.overwrite)
                elif kind == "resample":
                    _, prefilter, target = plan
                    tmp1, tmp2 = temp_wav(), temp_wav()
                    run_ffmpeg(input_path, tmp1, prefilter, "pcm_f32le",
                               new_title, overwrite=True)
                    run_sox(tmp1, tmp2, ["rate", "-v", str(target)])
                    run_ffmpeg(tmp2, output_path, None, out_codec,
                               new_title, overwrite=args.overwrite,
                               metadata_from=input_path)
                elif kind == "pitch":
                    _, pitched_rate, cents, target = plan
                    tmp1, tmp2 = temp_wav(), temp_wav()
                    run_ffmpeg(input_path, tmp1, None, "pcm_f32le",
                               new_title, overwrite=True)
                    effects = ["rate", str(pitched_rate), "pitch", repr(cents)]
                    if target is not None:
                        effects += ["rate", "-v", str(target)]
                    run_sox(tmp1, tmp2, effects)
                    run_ffmpeg(tmp2, output_path, None, out_codec,
                               new_title, overwrite=args.overwrite,
                               metadata_from=input_path)
        finally:
            if tempdir is not None:
                shutil.rmtree(tempdir, ignore_errors=True)
        return 0

    except subprocess.CalledProcessError as e:
        print(f"\nffmpeg/sox failed with exit code {e.returncode}", file=sys.stderr)
        return 1

    except FileNotFoundError:
        print("Error: 'ffmpeg'/'sox' not found. Is ffmpeg installed (and sox for --pitch/resampling)?", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
