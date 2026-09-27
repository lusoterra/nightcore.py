# nightcore.py

This is a ~~simple~~ wrapper that creates bit-perfect Nightcore by altering the
audio to change speed/pitch file using FFmpeg, SoX and rubberband.

[![asciicast](https://asciinema.org/a/UqwG1b66mSsPeLCk.svg)](https://asciinema.org/a/UqwG1b66mSsPeLCk)

Lots of autism went into this to make the most pristine Nightcore possible using
state-of-the-art, modern audio processing tools.

SoX is used for resampling stages, rubberband for pitch-changing stages and
FFmpeg as a general tool for everything else.

Most of the flags here should cover 99% of all Nightcore tracks from the 2000s.

## Some stuff you can do with this script:

- Speed songs up or slow them down bit-perfectly (-s 1.2) - pitch and tempo move together, this is the classic Nightcore effect
- Resample to player-safe rates with -R ([88.2kHz], [96kHz]...), or keep both copies with -B/-BR
- Pitch-shift without changing duration (-s 1.2 -p, -S 3.16st), or combine speed and pitch in one run (-s 1.2 -S 2.16)
- Undo anything from the filename tags with -D (Song [1.2x].flac → original, quality tags kept)
- Share-friendly lossy encodes (-F mp3/opus/aac) - you pick the quality, the script picks the best encoder available
- Convert whole folders with --batch: reruns skip finished files, -y redo them all
- Retime `.lrc` lyrics to match sped-up audio; they land next to the music with the same name
- Frequency calculators built in: --pitch-diff and --speed-diff turn measured peaks into exact semitones
- Filenames and titles document every job ([1.2x], [3.16st], [16bit 44.1kHz]), and --simple-title gives players a clean Title tag
- Safe by default: dry-run with -n, never overwrites without -y, Ctrl+C deletes partials instead of leaving corrupt files

## Requirements

Have all of these installed for the smoothest experience:

- python3
- FFmpeg
- SoX
- rubberband
- flac (metaflac, because FFmpeg WILL screw up your tags, have this installed)
- qaac/qaac-wine (optional, for ALAC/AAC, FFmpeg fallback)
- lame (optional, MP3 fallback when FFmpeg lacks libmp3lame)
- opus-tools (optional, preferred Opus encoder, ffmpeg libopus fallback)

## Usage

```
usage: nightcore [-h] [-s SPEED] [-r] [-y] [-q] [-R] [-B] [-F FMT]
                    [--lossy-opts OPTS] [--batch [DIR]] [-p] [-S ST] [-n]
                    [--simple-title [TEXT]] [--autotag] [--autorename]
                    [--tag-template FILE] [--cover IMG]
                    [--pitch-diff F [F ...]] [-D] [--speed-diff ST]
                    [--rb-params PARAMS]
                    [input]

Change audio speed/pitch using ffmpeg, sox and rubberband

positional arguments:
  input                   Input audio file (not needed for --pitch-diff/--speed-diff;
                          .lrc lyrics allowed with -s: retimed copy)

options:
  -h, --help              show this help message and exit
  -s, --speed SPEED       Speed factor (1.2 = 20% faster, 0.8 = slower)
  -r, --reciprocal        Use 1/speed instead (undo a speedup)
  -y, --overwrite         Overwrite existing output files
  -q, --quiet             Drop tool output (-q keeps stage/progress lines, -qq keeps
                          only warnings and errors; a failed tool's captured output
                          always prints)
  -R, --resample          Resample to next standard rate via SoX
                          (default: keep non-standard rate, bit-perfect)
  -B, --both              With -R, also keep the non-standard-rate file
  -F, --format FMT        Output format: flac (default), alac, wav, mp3, opus, aac
  --lossy-opts OPTS       REQUIRED for lossy outputs: encoder quality flags
                          (shlex-split, you choose, = form only, e.g.
                          --lossy-opts="-q:a 0" or --lossy-opts="--bitrate 128").
                          Flag dialect picks the encoder: native-CLI style (-V,
                          --bitrate, ...) uses lame/opusenc/qaac, -X:a style uses ffmpeg
  --batch [DIR]           Convert every audio file in DIR (default: .) instead of
                          one input; outputs go to ./encodes/[format]/
  -p, --pitch             Pitch-shift only, keep duration (needs rubberband;
                          a modifier on -s/-S: the amount comes from there)
                          Labelled [Nst] full precision; negating reverses it
  -S, --semitones ST      Pitch-shift by ST semitones (alone: pitch-only;
                          with -s: speed+pitch combo like [1.2x 2.16st])
  -n, --dry-run           Print output names, convert nothing
  --simple-title [TEXT]   Keep the Title tag free of script-added [...] groups
                          (filenames keep them); optional TEXT is appended after
                          a space, e.g. --simple-title="(Nightcore)"
                          (bare flag goes last, or use --simple-title=)
  --autotag               Append the bracket tags ([1.2x], rate, ...) to the Title tag
                          (default: leave Title as-is; --simple-title overrides this)
  --autorename            Rename output to 'Artist - Title [tags]' from tags
                          (default: keep the input filename, append params)
  --tag-template FILE     Tag template file (key="value" lines, multiline ok).
                          Empty/bare values keep the source tag; tags missing
                          from the file are stripped from outputs.
  --cover IMG             PNG/JPG artwork: sole attached picture of outputs.
  --pitch-diff F [F ...]  Calculator, no conversion: 1 speed or 2 frequencies
                          -> semitones, cents, speed factor
  -D, --denightcore       Undo from filename tags: reads [Nx]/[Nst], removes them
                          Keeps quality tags; fails if no shift tags found
  --speed-diff ST         Calculator, no conversion: semitones -> speed factor
  --rb-params PARAMS      Extra rubberband flags, appended after the defaults
                          (-3 -p ST); a -p/--pitch inside overrides the pitch
                          amount (warns) instead of duplicating it, e.g. --formant
                          (use the = form: --rb-params="--formant")
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

## Transparent lossy settings

| Codec | Encoder (flag dialect that selects it)            | Transparent setting | Rough size                             |
|-------|---------------------------------------------------|---------------------|----------------------------------------|
| MP3   | libmp3lame (`-q:a 0`) / lame (`-V 0`)                 | V0 VBR              | ~245 kbps                              |
| Opus  | opusenc (`--bitrate 128`) / libopus (`-b:a 128k`)     | 128k VBR            | 128 kbps                               |
| AAC   | qaac (`-V 91` TVBR)                                 | True VBR Q91        | ~100-150 kbps, content-dependent       |
| AAC   | ffmpeg (`-b:a doNot`)                               | use this            | 1 hundred million kilobits per second* |

\**seriously if you're going to encode AAC use qaac/afconvert*
