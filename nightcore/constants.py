"""Shared tables, regexes and filename-tag vocabularies."""

import re

# Provenance stamp (--no-stamp disables): written to this tag on every output, and
# the project URL embedded in the stamp text (also in pyproject.toml).
# Pipeline stamp (--no-pipeline disables): machine-readable encode record
# (shift, resample, format, source fingerprint, tool versions) that -D reads
# back. Separate tag so branding and data can be controlled independently.
# "nightcore.py" names the project; bare "nightcore" is the binary.
STAMP_TAG = "description"
PIPELINE_TAG = "encoder_settings"
# Magic marker prefixing every pipeline record we write. Readers refuse
# records without it ("magic number not found") so foreign tags that
# happen to look like ours are never acted on. -N adds it to old
# records it touches; pre-magic files gain it via a retag round.
PIPELINE_MAGIC = "NiGhTcOrE"
PROJECT_URL = "https://git.lusosterone.net/lev/nightcore"

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


# Output formats: extension + encoder selection. Automatic quality defaults
# exist only for lossless output, tuned for most compression (FLAC level
# 12; ALAC has no knobs -- its default is already optimal).
# Lossy formats prefer the best native frontend (verified binary, ffmpeg
# fallback) but carry zero baked-in quality: --lossy-opts is mandatory
# for them (fail loudly otherwise), and rates are left alone unless -R
# is given -- the encoder choking on a weird rate is the user's signal
# to resample or retune it themselves.
# WAV is input-only (no tags or pictures survive its muxer, so -D could
# never read it back and covers would silently vanish).
# "wav" used a depth-matched PCM codec (see wav_codec_for(), still used
# for temp WAVs and depth matching).
OUTPUT_FORMATS = {
    "flac": {"ext": ".flac", "codec": "flac", "lossy": False,
             "ffmpeg_args": ["-compression_level", "12"]},
    "alac": {"ext": ".m4a", "codec": "alac", "lossy": False},
    "mp3": {"ext": ".mp3", "codec": "libmp3lame", "lossy": True},
    "opus": {"ext": ".opus", "codec": "libopus", "lossy": True},
    "aac": {"ext": ".m4a", "codec": "aac", "lossy": True},
}


# Lossy quality defaults per (format, backend kind): applied only when
# the user passes no --lossy-opts, which then replaces them wholesale
# (appending would hand qaac duplicate -V flags, which it rejects).
# ffmpeg AAC has no default worth using: None fails loudly instead.
LOSS_DEFAULTS = {
    ("mp3", "ffmpeg"): ["-q:a", "0"],
    ("mp3", "lame"): ["-V", "0"],
    ("opus", "opusenc"): ["--bitrate", "128"],
    ("opus", "ffmpeg"): ["-b:a", "128k"],
    ("aac", "qaac"): ["-V", "91"],
    ("aac", "ffmpeg"): None,
}


# Extensions scanned by --batch.
AUDIO_EXTS = (".flac", ".wav", ".aiff", ".aif", ".ape", ".wv", ".tta",
              ".alac", ".m4a", ".mp3", ".ogg", ".oga", ".opus", ".aac",
              ".wma", ".mpc", ".ac3", ".dts", ".amr", ".speex", ".mp2",
              ".lrc")


_SHIFT_TOKEN = re.compile(r"^(-?\d+(?:\.\d+)?)(x|st)$")


_RATE_TOKEN = re.compile(r"^\d+(?:\.\d+)?kHz$")


# Bracket quality tags our own naming emits: "[16bit 44.1kHz]" and
# "[mp3 CBR 320k LAME3.100]" shapes (codec spellings cover the displayed
# dash-normalized variants too, e.g. "amr-nb").
_QUALITY_CODECS = {c.replace("_", "-") for c in LOSSY_CODECS} | LOSSY_CODECS


_BITRATE_TAG = re.compile(
    r"^\[(?:" + "|".join(sorted(_QUALITY_CODECS)) + r")"
    r"(?:\s+(?:VBR|CBR))?\s+\d+k(?:\s+.*)?\]$", re.IGNORECASE)


_DEPTH_TAG = re.compile(r"^\[\d+bit\s+\d+(?:\.\d+)?kHz\]$", re.IGNORECASE)


# LRC lyric timestamps: [mm:ss], [mm:ss.cc] (centiseconds) or [mm:ss.ccc]
# (milliseconds); the separator may be '.' or ':'. Metadata tags like
# [ar:Artist] never match (letters before the colon).
_LRC_STAMP = re.compile(r"\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\]")


_LRC_LENGTH = re.compile(
    r"^(\s*)\[length:(\d+):(\d{2})(?:[.:](\d{1,3}))?\](\s*)$",
    re.IGNORECASE)


# Source extensions preferred when pairing .lrc lyrics with their
# parent audio (same stem): lossless masters first, then anything else.
_LOSSLESS_EXTS = {".flac", ".alac", ".wav", ".aiff", ".aif",
                  ".ape", ".wv", ".tta"}


# Bare short flags that only make sense to the native CLI frontend.
# (Any --long-opt also votes CLI; any -X:a stream option or ffmpeg-only
# name votes ffmpeg; anything else, e.g. -ar/-ac, is ffmpeg-side too --
# the CLI tools have no such flags. See README for the dialect table.)
CLI_SHORT_OPTS = {
    "mp3": {"-V", "-q", "-b", "-m"},
    "opus": set(),
    "aac": {"-V", "-v", "-c", "-a", "-q"},
}


FFMPEG_ONLY_OPTS = {
    "-ar", "-ac", "-ab", "-vbr", "-application", "-frame_duration",
    "-packet_loss", "-mapping_family", "-aac_coder", "-aac_ms", "-aac_is",
    "-aac_pns", "-cutoff",
}


_DIALECT_EXAMPLES = {
    "mp3": ('lame-style ("-V 0")', 'libmp3lame-style ("-q:a 0")'),
    "opus": ('opusenc-style ("--bitrate 128")', 'libopus-style ("-b:a 128k")'),
    "aac": ('qaac-style ("-V 91")', 'ffmpeg-style ("-b:a 128k")'),
}


# Attached-picture codecs safe to pass through (ffmpeg copy) or hand to qaac.
COVER_CODECS = {"mjpeg": "jpg", "png": "png"}


# Image file suffixes and their MIME types (for --cover validation and
# metaflac picture imports).
_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


# Source encoder tags that must not survive a lossy re-encode: they
# describe how the SOURCE was made, not the output (ffmpeg's own fresh
# encoder tag, if any, is unaffected).
STALE_TAGS = ("encoder", "encoding_settings")


# Extensions whose muxers carry attached pictures (wav cannot).
COVER_FORMATS = (".flac", ".m4a", ".mp3", ".opus")


# Template key spellings some muxers don't store (movenc drops bare
# "albumartist" but keeps "album_artist").
_TAG_ALIASES = {"albumartist": "album_artist"}


# Cover-art filenames (case-insensitive stem + image suffix) copied
# alongside batch outputs so archived folders stay complete.
COVER_ART_NAMES = {"cover", "folder", "front", "albumart", "artwork"}


COVER_ART_EXTS = {".jpg", ".jpeg", ".png"}


