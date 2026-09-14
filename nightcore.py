#!/usr/bin/env python3

import argparse
import subprocess
import sys
from pathlib import Path

STANDARD_SAMPLE_RATES = [
    8000, 11025, 16000, 22050, 32000,
    44100, 48000, 88200, 96000,
    176400, 192000, 352800, 384000,
]


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
        "--resample",
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

    # Build filename/title suffix: omit speed when -s 1, append [96kHz] on resample.
    speed_suffix = "" if speed == 1.0 else f" [{speed:g}x]"
    rate_suffix = f" [{format_rate(target_rate)}]" if target_rate is not None else ""
    suffix = speed_suffix + rate_suffix

    if not suffix:
        print(
            "Error: nothing to do (speed is 1 and no resampling requested)",
            file=sys.stderr,
        )
        return 1

    stem = input_path.stem
    # Preserve input directory and extension instead of forcing CWD/.flac.
    output_path = input_path.parent / (stem + suffix + input_path.suffix)

    if output_path == input_path:
        print("Error: output would overwrite input, aborting", file=sys.stderr)
        return 1

    if output_path.exists() and not args.overwrite:
        print(
            f"Error: output already exists: {output_path} (use -y to overwrite)",
            file=sys.stderr,
        )
        return 1

    if target_rate is not None:
        if speed == 1.0:
            audio_filter = f"aresample={target_rate}"
        else:
            audio_filter = (
                f"asetrate={sample_rate}*{speed},aresample={target_rate}"
            )
    else:
        audio_filter = f"asetrate={sample_rate}*{speed}"

    original_title = get_title(input_path, fallback=stem)
    new_title = original_title + suffix

    ffmpeg_cmd = [
        "ffmpeg",
        "-nostdin",
        "-y" if args.overwrite else "-n",
        "-i", str(input_path),
        "-af", audio_filter,
        "-metadata", f"title={new_title}",
        str(output_path)
    ]

    try:
        subprocess.run(ffmpeg_cmd, check=True)
        return 0

    except subprocess.CalledProcessError as e:
        print(f"\nffmpeg failed with exit code {e.returncode}", file=sys.stderr)
        return 1

    except FileNotFoundError:
        print("Error: 'ffmpeg' not found. Is ffmpeg installed?", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
