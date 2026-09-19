# nightcore.py

This is a simple wrapper that creates bit-perfect Nightcore by altering the
sample rate of the audio to change speed/pitch file using FFmpeg.

Output is always lossless (FLAC by default) to avoid generational loss. Lossy
sources (mp3, opus, …) are tagged in the filename and title,
e.g. song [1.2x] [mp3 320k].flac. For MP3 the tag also shows the encoding
mode and encoder when detectable (e.g. `[mp3 CBR 320k LAME3.100]`,
`[mp3 VBR 244k]` - VBR bitrates are file averages); opus CBR/VBR is read
from packet-size variance (e.g. `[opus VBR 128k]`); other codecs show the
encoder only when informative (muxer noise like `Lavf`, or an encoder
merely restating the codec, is omitted; vorbis and AAC expose no reliable
VBR/CBR signal, so they get no mode label).
The Title tag is the source file's embedded title plus the same suffix
(falls back to the filename when the source has no embedded title).

## Requirements

- python3
- FFmpeg
- SoX (Sound eXchange)

## Usage

```
usage: nightcore [-h] [-s SPEED] [-r] [-y] [-R] [--target-rate HZ] [-B]
                    [-F FMT] [-p] [-S ST] [--pitch-diff F1 F2]
                    [input]

Change audio speed/pitch using ffmpeg and sox

positional arguments:
  input               Input audio file (not needed with --pitch-diff)

options:
  -h, --help         show this help message and exit
  -s, --speed SPEED  Speed (e.g. 1.2 = 20% faster, 0.8 = 20% slower)
  -r, --reciprocal   Reciprocal (e.g. 1.2 becomes 1/1.2 as the multiplier)
  -y, --overwrite    Overwrite output file if it exists (default: fail if exists)
  -R, --resample     Resample output to the next higher standard sample rate
                     for player compatibility (default: keep the non-standard
                     sample rate, bit-perfect for archival).
  --target-rate HZ   Explicit resample target in Hz (implies resampling).
                     Overrides --resample auto-selection.
  -B, --both         With -R/--resample or --target-rate, generate both files:
                     the non-standard-rate file and the standard-rate file.
  -F, --format FMT   Lossless output format (default: flac).
                     alac = Apple Lossless (.m4a),
                     wav = Windows-friendly PCM (.wav, bit depth matched).
  -p, --pitch        Pitch-shift only (duration preserved) instead of speeding up.
                     e.g. -s 1.1 shifts pitch by 1.65st (needs sox).
                     Labelled [1.65st], not [1.1x].
  -S, --semitones ST Shift amount in semitones instead of -s/--speed
                     (speed = 2^(ST/12); e.g. 3.16 ~= 1.2x).
  --pitch-diff F1 F2 Print the pitch difference between two peak frequencies
                     (F1 = original, F2 = pitched: semitones, cents, speed) and exit.
```

## To figure out pitched* up (instead of spedup nightcore) semitones...

Measure the spectral peak of the original track and of the pitched track
at the same position (see [Nightcore Semitone Detection](Nightcore%20Semitone%20Detection.pdf)
for a walkthrough), then:

```
nightcore --pitch-diff 54 63        # 2.67st, speed 1.1667x
nightcore song.flac -S 2.67 -p -y   # remake it: song [2.67st].flac
```

\*of course speeding up also pitches up audio, but I'm talking about nightcore
tracks that have the same length in seconds but a higher pitch.
