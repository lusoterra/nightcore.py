# nightcore.py

This is a simple wrapper that creates bit-perfect Nightcore by altering the
sample rate of the audio to change speed/pitch file using FFmpeg.

Output is always lossless (FLAC by default) to avoid generational loss.
Lossy sources (mp3, opus, …) are tagged in the filename and title,
e.g. `song [1.2x] [mp3 320k].flac`. The Title tag is the source file's
embedded title plus the same suffix (falls back to the filename when the
source has no embedded title).

## Usage

```
usage: nightcore [-h] [-s SPEED] [-r] [-y] [-R] [--target-rate HZ] [-B] [-F FMT] input

Change audio speed using ffmpeg

positional arguments:
  input              Input audio file

options:
  -h, --help         show this help message and exit
  -s, --speed SPEED  Speed (e.g. 1.2 = 20% faster, 0.8 = 20% slower)
  -r, --reciprocal   Reciprocal (e.g. 1.2 becomes 1/1.2 as the multiplier)
  -y, --overwrite    Overwrite output file if it exists (default: fail if exists)
  -R, --resample     Resample output to the next higher standard sample rate
                     for player compatibility (default: keep weird asetrate rate
                     for bit-perfect archival).
  --target-rate HZ   Explicit resample target in Hz (implies resampling).
                     Overrides --resample auto-selection.
  -B, --both         With -R/--resample or --target-rate, generate both files:
                     the weird asetrate-rate file and the standard-rate file.
  -F, --format FMT   Lossless output format (default: flac).
                     alac = Apple Lossless (.m4a),
                     wav = Windows-friendly PCM (.wav, bit depth matched).
```
