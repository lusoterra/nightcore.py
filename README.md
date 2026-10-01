# nightcore.py

This is a ~~simple~~ wrapper that creates bit-perfect Nightcore by altering the
audio to change speed/pitch file using FFmpeg, SoX and rubberband.

[demo](nightcore.cast)

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
- FFmpeg (incl. ffprobe, used for probing and transcoding)
- SoX
- rubberband
- flac (metaflac reads/writes FLAC tags exactly, because FFmpeg WILL screw up your tags, have this installed)
- opus-tools (opusenc encodes, opusinfo reads Opus tags that ffprobe cannot parse)
- vorbis-tools (vorbiscomment reads Ogg tags)
- lame (optional, MP3 fallback when FFmpeg lacks libmp3lame)
- qaac/qaac-wine (optional, for ALAC/AAC, FFmpeg fallback)

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
| MP3   | libmp3lame (`-q:a 0`) / lame (`-V 0`)                 | V0 VBR              | ~245 kbps                              |
| Opus  | opusenc (`--bitrate 128`) / libopus (`-b:a 128k`)     | 128k VBR            | 128 kbps                               |
| AAC   | qaac (`-V 91` TVBR)                                 | True VBR Q91        | ~100-150 kbps, content-dependent       |
