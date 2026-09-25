# nightcore.py

This is a simple wrapper that creates bit-perfect Nightcore by altering the
sample rate of the audio to change speed/pitch file using FFmpeg.

It can also independently pitch up audio with SoX and it automatically upsamples
the track based on the semitone shift to preserve frenquencies that would
otherwise be lost.

You can also encode music in a standard sample rate like (ex: 48kHz, 88.2kHz, 96kHz...)
automatically to be chosen to be the smallest standard frenquency that's still
higher than the frenquency that FFmpeg's `asetrate` changed.

Most of the flags here should cover 99% of all nightcore tracks from the 2000s.

## Requirements

- python3
- FFmpeg
- SoX (Sound eXchange)
- qaac (optional, for ALAC, FFmpeg fallback)

## Usage

```
usage: nightcore [-h] [-s SPEED] [-r] [-y] [-R] [-B] [-F FMT] [-p] [-S ST] [-n]
                 [--pitch-diff F [F ...]] [-D] [--speed-diff ST] [input]

Change audio speed/pitch using ffmpeg and sox

positional arguments:
  input                   Input audio file (not needed for --pitch-diff/--speed-diff)

options:
  -h, --help              show this help message and exit
  -s, --speed SPEED       Speed factor (1.2 = 20% faster, 0.8 = slower)
  -r, --reciprocal        Use 1/speed instead (undo a speedup)
  -y, --overwrite         Overwrite existing output files
  -R, --resample          Resample to next standard rate via SoX
                          (default: keep non-standard rate, bit-perfect)
  -B, --both              With -R, also keep the non-standard-rate file
  -F, --format FMT        Output format: flac (default), alac (.m4a), wav
  -p, --pitch             Pitch-shift only, keep duration (needs sox)
                          Labelled [Nst] full precision; negating reverses it
  -S, --semitones ST      Pitch-shift by ST semitones (alone: pitch-only;
                          with -s: speed+pitch combo like [1.2x 2.16st])
  -n, --dry-run           Print output names, convert nothing
  --pitch-diff F [F ...]  Calculator, no conversion: 1 speed or 2 frequencies
                          -> semitones, cents, speed factor
  -D, --denightcore       Undo from filename tags: reads [Nx]/[Nst], removes them
                          Keeps quality tags; fails if no shift tags found
  --speed-diff ST         Calculator, no conversion: semitones -> speed factor
```

## Figuring out semitone differences for pitched up nightcore

Measure the spectral peak of the original track and of the pitched track
at the same position (see [Nightcore Semitone Detection](Nightcore%20Semitone%20Detection.pdf)
for a walkthrough), then:

```
nightcore --pitch-diff 54 63        # 2.6687090560373763st, speed 1.1666666666666667x
nightcore song.flac -S 2.6687090560373763 -p -y   # remake it: song [2.6687090560373763st].flac
nightcore song.flac -s 1.2 -S 2.166866948 -y  # both: song [1.2x 2.166866948st].flac
```
