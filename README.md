# nightcore.py

This is a simple wrapper that creates bit-perfect Nightcore by altering the
sample rate of the audio to change speed/pitch file using FFmpeg.

It can also independently pitch up audio with SoX and it automatically upsamples
the sample rate of the file based on the semitone shift to preserve frenquencies
that would otherwise be lost.

You can also encode music in a standard sample rate like 48kHz, 88.2kHz
automatically to be chosen to be the smallest standard frenquency that's still
higher than the frenquency that FFmpeg's asetrate changed.

Most of the flags here should cover 99.9% of all nightcore tracks from the 2000s
implementing edge cases such as <abbr title="Master Blaster - Everywhere (DJ Splash Remix) [1.2x then 2.17st] [opus VBR 164k].flac">[1.2x then 2.17st]</abbr> (first sped up, then
pitched) would be pointless.

## Requirements

- python3
- FFmpeg
- SoX (Sound eXchange)
- qaac (optional, for ALAC, FFmpeg fallback)

## Usage

```
usage: nightcore [-h] [-s SPEED] [-r] [-y] [-R] [-B] [-F FMT] [-p] [-S ST]
                    [-n] [--pitch-diff F [F ...]] [--speed-diff ST]
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
  -B, --both         With -R/--resample, generate both files:
                     the non-standard-rate file and the standard-rate file.
  -F, --format FMT   Lossless output format (default: flac).
                     alac = Apple Lossless (.m4a),
                     wav = Windows-friendly PCM (.wav, bit depth matched).
  -p, --pitch        Pitch-shift only (duration preserved) instead of speeding up.
                     e.g. -s 1.1 shifts pitch by 1.65st (needs sox).
                     Labelled [1.65st], not [1.1x].
  -S, --semitones ST Pitch-shift by ST semitones (implies pitch-only mode,
                     same as giving the equivalent -s with -p; e.g. 3.16 ~= 1.2x).
  --pitch-diff F [F ...]
                        Pitch difference calculator (prints semitones, cents, speed).
                        One value = speed factor (e.g. 1.36); two values = peak
                        frequencies F1 original, F2 pitched. Exits without converting.
  --speed-diff ST       Reverse calculator: semitones ST to speed factor and cents.
                        Exits without converting.
  -n, --dry-run         Print the output filename(s) that would be generated
                        and exit without converting anything (for bulk renaming;
                        pair with puddletag to rewrite tags from filenames).
```

## To figure out pitched* up (instead of spedup nightcore) semitones...

Measure the spectral peak of the original track and of the pitched track
at the same position (see [Nightcore Semitone Detection](Nightcore%20Semitone%20Detection.pdf)
for a walkthrough), then:

```
nightcore --pitch-diff 54 63        # 2.6687090560373763st, speed 1.1666666666666667x
nightcore song.flac -S 2.67 -p -y   # remake it: song [2.67st].flac
```

\*of course speeding up also pitches up audio, but I'm talking about nightcore
tracks that have the same length in seconds but a higher pitch.
