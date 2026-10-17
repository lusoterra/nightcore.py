# nightcore.py

> Windows users: install [vc_redist.x64.exe](https://download.visualstudio.microsoft.com/download/pr/bd1c8d9d-ba95-4eee-bc6e-df1fcc876373/CC0FF0EB1DC3F5188AE6300FAEF32BF5BEEBA4BDD6E8E445A9184072096B713B/VC_redist.x64.exe) if qaac fails to check CoreAudioToolbox.

This is a ~~simple~~ wrapper that creates bit-perfect Nightcore by altering the
audio to change speed/pitch file using FFmpeg, SoX and rubberband.

[![asciicast](https://asciinema.org/a/UqwG1b66mSsPeLCk.svg)](nightcore.cast)

Lots of autism went into this to make the most pristine Nightcore possible using
state-of-the-art, modern audio processing tools.

SoX is used for resampling stages, rubberband for pitch-changing stages and
FFmpeg as a general tool for everything else.

It has every feature you'd possibly want to automate the (re)making and tagging
of Nightcore. You can, of course, slow down music with it as well.

## Some stuff you can do with this script:

- Speed songs up or slow them down bit-perfectly (-s 1.2) - pitch and tempo move together, this is the classic Nightcore effect
- Resample to player-safe rates with -R ([88.2kHz], [96kHz]...), batch -R sorts them into std-samplerate/[codec]/
- Pitch-shift without changing duration (-s 1.2 -p, -S 3.16st), or combine speed and pitch in one run (-s 1.2 -S 2.16)
- Undo anything with -D (reads the pipeline tag, never the filename; quality tags kept)
- Share-friendly lossy encodes (-F mp3/opus/aac) - you pick the quality, the script picks the best encoder available
- Convert whole folders with --batch: reruns skip finished files, -y redo them all
- Retime `.lrc` lyrics to match sped-up audio; they land next to the music with the same name
- `--mkvid`: remux one song (+ optional `.lrc` lyrics, + image) into a
  1080p YouTube video. The image is letterboxed/pillarboxed into 1920x1080
  with black bars (never stretched) and the audio rides losslessly -- FLAC
  in mkv, ALAC in mp4 -- so YouTube's Opus transcode is one generation of
  loss; no resampling happens. `-F mkv` (default) or `-F mp4`. Without
  `--cover` the song's embedded cover art is used; no image at all fails
  loudly. `.lrc` lyrics are embedded as a subtitle track, and all audio
  tags (title, artist, album, date, genre ...) carry over. No audio
  processing flags apply (it is a remux, not a conversion).
- Frequency calculators built in: --pitch-diff and --speed-diff turn measured peaks into exact semitones
- Filenames mirror titles: `Artist - Title [Nightcore 2.17st].flac` (rounded shift, no quality clutter — that lives in the pipeline tag; `[rate]` stays when resampling, `[denightcored BP/NBP]` on undos; customize on the fly with --autorename), titles carry just `Title [Nightcore 2.17st]` by default (rounded to 2 decimals), and `title=`/`artist=` lines in the tag template accept bare `{title}`/`{diff}`/`{quality}`/`{resample}`/`{denightcore}` slots (`{diff(2)}` rounds for display) — an explicit `title=` is used verbatim. Artist and all other source tags ride through untouched by default
- Undo anything with -D (pipeline-trusted, filenames aren't): names rebuild as `Artist - Title [quality] [denightcored BP/NBP]`, the pipeline records the undo so double-undos refuse, and pure-speedup cycles are bit-exact
- Every output carries two marks by default: a `Made with nightcore.py` DESCRIPTION (branding, `--no-stamp` drops it) and an opusenc-style `encoder_settings` pipeline tag (shift, resample, source fingerprint, tool versions — what `-D` reads; `--no-pipeline` drops it independently)
- Safe by default: dry-run with -n, never overwrites without -y, Ctrl+C finishes the current file then stops (twice aborts and deletes partials) instead of leaving corrupt files

## Requirements

Have all of these installed for the smoothest experience:

- python3
- FFmpeg (incl. ffprobe, used for probing and transcoding)
- SoX
- rubberband
- flac (metaflac reads/writes FLAC tags exactly, because FFmpeg WILL screw up your tags, have this installed)
- opus-tools (opusenc encodes, opusinfo reads Opus tags that ffprobe cannot parse)
- vorbis-tools (vorbiscomment reads Ogg tags)
- lame (preferred MP3 encoder; FFmpeg libmp3lame fallback)
- qaac/qaac-wine (optional, for ALAC/AAC, FFmpeg fallback)

Run `nightcore --check` to verify every tool, encoder and tag reader at
once (same report on Linux and the Windows builds, which carry their
tools in `tools\` next to the exe).

## Figuring out semitone differences for pitched up nightcore

Measure the spectral peak of the original track and of the pitched track
at the same position (see [Nightcore Semitone Detection](https://lusosterone.net/blogs/nightcore-semitone-detection/)
for a walkthrough), then:

```
nightcore --pitch-diff 54 63        # 2.6687090560373763st, speed 1.1666666666666667x
nightcore song.flac -S 2.6687090560373763 -p -y   # remake it: song [2.6687090560373763st].flac
nightcore song.flac -s 1.2 -S 2.166866948 -y  # both: song [1.2x 2.166866948st].flac
```

## Transparent lossy settings

| Codec | Encoder (flag dialect that selects it)            | Transparent setting | Rough size                             |
|-------|---------------------------------------------------|---------------------|----------------------------------------|
| MP3   | lame (`-V 0`) / libmp3lame (`-q:a 0`)                 | V0 VBR              | ~245 kbps                              |
| Opus  | opusenc (`--bitrate 128`) / libopus (`-b:a 128k`)     | 128k VBR            | 128 kbps                               |
| AAC   | qaac (`-V 91` TVBR)                                 | True VBR Q91        | ~100-150 kbps, content-dependent       |
