#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
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
        description="Change audio speed using ffmpeg",
        formatter_class=argparse.RawTextHelpFormatter
    )

    parser.add_argument(
        "input",
        type=str,
        help="Input audio file"
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
            "for player compatibility (default: keep weird asetrate rate\n"
            "for bit-perfect archival)."
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
            "the weird asetrate-rate file and the standard-rate file."
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
            "-show_entries", "format=bit_rate",
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

        codec = stream.get("codec_name")
        codec = str(codec).lower() if codec else None

        bit_rate = None
        for candidate in (stream.get("bit_rate"), (info.get("format") or {}).get("bit_rate")):
            if candidate is None:
                continue
            try:
                bit_rate = int(str(candidate).strip())
            except (ValueError, AttributeError):
                continue
            if bit_rate and bit_rate > 0:
                break
            bit_rate = None

        return codec, bit_rate, bits_from_stream(stream)

    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None, None, None


def lossy_suffix_for(codec, bit_rate):
    """Build ' [codec bitratek]' tag for lossy sources, else ''.

    Returns '' when codec is not a known lossy codec. When the bitrate
    is unknown, returns ' [codec]' without a bitrate.
    """
    if codec not in LOSSY_CODECS:
        return ""
    # Normalize amr variants for display: amr_nb -> amr, etc.
    display_codec = codec.replace("_", "-")
    if bit_rate is None:
        return f" [{display_codec}]"
    kbps = int(round(bit_rate / 1000))
    if kbps <= 0:
        return f" [{display_codec}]"
    return f" [{display_codec} {kbps}k]"


def get_title(input_path, fallback):
    try:
        ffprobe_cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format_tags=title",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(input_path)
        ]

        result = subprocess.run(
            ffprobe_cmd,
            capture_output=True,
            text=True,
            check=True
        )

        title = result.stdout.strip()

        return title if title else fallback

    except (subprocess.CalledProcessError, FileNotFoundError):
        return fallback


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


def run_ffmpeg(input_path, output_path, audio_filter, codec, title, overwrite):
    """Run one ffmpeg transcode; raises CalledProcessError on failure."""
    ffmpeg_cmd = [
        "ffmpeg",
        "-nostdin",
        "-y" if overwrite else "-n",
        "-i", str(input_path),
    ]
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
    input_path = Path(args.input)

    if not input_path.is_file():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        return 1

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
    # "<stem> [<speed>x] [<codec> <bitrate>k] [<rate>]<ext>"
    # - speed omitted when -s 1
    # - lossy tag added when the source codec is lossy (to flag that the
    #   output is a lossless container around a lossy source)
    # - rate appended on resample.
    codec, bit_rate, bits = get_audio_info(input_path)
    lossy_suffix = lossy_suffix_for(codec, bit_rate)
    speed_suffix = "" if speed == 1.0 else f" [{speed:g}x]"
    rate_suffix = f" [{format_rate(target_rate)}]" if target_rate is not None else ""
    weird_suffix = speed_suffix + lossy_suffix
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

    def build_filter(target):
        if target is not None:
            if speed == 1.0:
                return f"aresample={target}"
            return f"asetrate={sample_rate}*{speed},aresample={target}"
        if speed == 1.0:
            # Pure container/codec conversion (e.g. mp3 -> flac): no filter.
            return None
        return f"asetrate={sample_rate}*{speed}"

    # Job list: (suffix, audio_filter). --both emits the weird-rate file
    # first, then the standard-rate file.
    if args.both and target_rate is not None:
        jobs = [
            (weird_suffix, build_filter(None)),
            (standard_suffix, build_filter(target_rate)),
        ]
    elif target_rate is not None:
        jobs = [(standard_suffix, build_filter(target_rate))]
    else:
        jobs = [(weird_suffix, build_filter(None))]

    # Output filenames: "<stem> [<speed>x] [<codec> <bitrate>k]
    # [<rate>]<ext>". The Title tag is the source file's embedded title
    # plus the same suffix (falls back to the filename when the source
    # has no title). Preserve input directory.
    outputs = [(suffix, input_path.parent / (stem + suffix + out_ext)) for suffix, _ in jobs]

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
        for (suffix, audio_filter), (_, output_path) in zip(jobs, outputs):
            new_title = original_title + suffix
            if total > 1:
                print(f"Writing {output_path}", file=sys.stderr)
            run_ffmpeg(input_path, output_path, audio_filter, out_codec,
                       new_title, overwrite=args.overwrite)
        return 0

    except subprocess.CalledProcessError as e:
        print(f"\nffmpeg failed with exit code {e.returncode}", file=sys.stderr)
        return 1

    except FileNotFoundError:
        print("Error: 'ffmpeg' not found. Is ffmpeg installed?", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
