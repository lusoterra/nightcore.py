"""External encoders: sox, rubberband, ffmpeg, lame, opusenc, qaac, metaflac."""

import os
import platform
import re
import shutil
import subprocess
import sys
import threading

from . import util
from .constants import CLI_SHORT_OPTS, FFMPEG_ONLY_OPTS, _DIALECT_EXAMPLES
from .tags import movenc_tag_map, read_all_tags
from .util import run_tool, say

def run_rubberband(input_wav, output_wav, semitones, extra_params):
    """Pitch-shift with rubberband (R3 engine); raises CalledProcessError."""
    run_tool(
        ["rubberband", "-3", "-p", repr(float(semitones))]
        + list(extra_params)
        + [str(input_wav), str(output_wav)],
    )


def wav_peak_db(path):
    """Peak level in dBFS of a wav via sox stats; None if unreadable."""
    try:
        result = subprocess.run(
            ["sox", str(path), "-n", "stats"],
            capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    match = re.search(r"(?m)^Pk lev dB\s+(.+)$", result.stderr or "")
    if not match:
        return None
    try:
        vals = [float(t)
                for t in re.findall(r"-?\d+(?:\.\d+)?", match.group(1))]
    except ValueError:
        return None
    return max(vals) if vals else None


def tame_hot_wav(path, make_temp):
    """Attenuate a rubberband temp peaking over 0dBFS; return wav to use.

    Rubberband (unlike SoX) does not clamp float output: if its
    auto-gain hits the floor, peaks can exceed full scale and would
    flat-top at the int encode. Trim to -1dBFS peak -- deterministic
    and minimal -- via make_temp(); files at/below full scale pass
    through untouched (no extra stage, bit-identical).
    Returns (path_to_use, trimmed).
    """
    peak = wav_peak_db(path)
    if peak is None or peak <= 0.0:
        return path, False
    gain_db = -(peak + 1.0)
    new_path = make_temp()
    say(f"  SoX anti-clip trim {gain_db:+.2f} dB "
        f"(peak was {peak:+.2f} dBFS)")
    run_sox(path, new_path, ["gain", f"{gain_db:.4f}"])
    return new_path, True


def run_sox_rate(input_path, output_path, target):
    """SoX VHQ resample, redoing guarded if it clips.

    SoX clamps overshoots (even float output) instead of preserving
    them, so hot masters would flat-top here. Its own "clipped N
    samples" warning is the detector: on the first occurrence the
    resample is redone guarded -- gain -h up front reserves headroom
    from the measured (inter-sample) peak, gain -rh at the end
    reclaims exactly that reservation, so the source level survives
    instead of coming back ~3dB quieter, and nothing flat-tops (the
    same pair -G would wrap the chain in, spelled out so the stage
    stays visible). Clean files take the single plain pass
    (bit-identical to before). Progress passes through live unless
    -q/--quiet. (Callers announce the stage line; only the guarded
    redo is announced here.)
    """
    proc = subprocess.Popen(
        ["sox", "-S", str(input_path), str(output_path),
         "rate", "-v", str(target)],
        start_new_session=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE)
    util._active_proc = proc
    chunks = []

    stderr = proc.stderr
    assert stderr is not None

    def _pump():
        while True:
            data = stderr.read(65536)
            if not data:
                break
            chunks.append(data)
            if not util._quiet:
                sys.stderr.buffer.write(data)
                sys.stderr.buffer.flush()

    pump = threading.Thread(target=_pump, daemon=True)
    try:
        pump.start()
        returncode = proc.wait()
    finally:
        util._active_proc = None
    pump.join()
    blob = b"".join(chunks)
    if returncode != 0:
        if util._quiet >= 1 and blob:
            sys.stderr.buffer.write(blob)
            sys.stderr.buffer.flush()
        raise subprocess.CalledProcessError(
            returncode, ["sox", "rate", str(target)])
    if b"clipped " in blob:
        say("  SoX clipped a hot master: redoing guarded "
            "(gain -h ... gain -rh, level kept)")
        run_sox(input_path, output_path, ["gain", "-h", "rate", "-v",
                                          str(target), "gain", "-rh"])
    return returncode


def run_sox(input_path, output_path, effects):
    """Run one sox effect chain with progress; raises CalledProcessError."""
    run_tool(
        ["sox", "-S", str(input_path), str(output_path)] + effects,
    )


def _verified_cli(binary, check_args, marker, timeout=60):
    """PATH + version-output check for an external encoder; bool."""
    if shutil.which(binary) is None:
        return False
    try:
        result = subprocess.run(
            [binary] + check_args,
            capture_output=True, text=True, check=False, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.SubprocessError, OSError):
        return False
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0 and marker in output


_qaac_version = None

# Raw `qaac --check` probe result (CompletedProcess, or None when the
# spawn itself failed). Stashed by find_qaac so reporters can inspect
# the exit code without re-spawning (wine startup isn't free).
_qaac_check_result = None

# Apple DLLs shipped by the full Windows setup/bundle for qaac
# (exact names from windows/build.sh APPLE).
_APPLE_DLLS = ("ASL.dll", "CoreAudioToolbox.dll", "CoreFoundation.dll",
               "icudt62.dll", "libdispatch.dll", "libicuin.dll",
               "libicuuc.dll", "objc.dll")

# Windows failed-to-start exit for a missing DLL in the load chain.
_DLL_NOT_FOUND = 0xC0000135


_tool_versions = {}

_VERSION_FLAGS = {
    "ffmpeg": "-version",
    "ffprobe": "-version",
    "sox": "--version",
    "rubberband": "--version",
    "lame": "--version",
    "opusenc": "--version",
    "opusinfo": "-h",
    "vorbiscomment": "--version",
    "metaflac": "--version",
    "qaac": "--version",
}

_VERSION_WORD = re.compile(r"version\s+([^\s)]+)")
_VERSION_TOKEN = re.compile(r"\S*\d\S*")


_which_cache = {}


def which_cached(name):
    """shutil.which with a run-level cache.

    Per-file PATH scans add up over batch runs (PATH entries are
    stat'ed on every call); tool presence cannot change mid-run, so
    one lookup per name is enough. Never raises.
    """
    if name not in _which_cache:
        try:
            _which_cache[name] = shutil.which(name)
        except Exception:
            _which_cache[name] = None
    return _which_cache[name]


def tool_version(name):
    """Compact version token for the pipeline tag ("n9.0.2", "4.0", ...).

    Prefers a "version X" word, else the first digit-bearing token of the
    version line. "unknown" when the tool is missing or unreadable. Never
    raises; results are cached (batch runs probe each tool once). qaac
    reuses the verified --check line instead of spawning wine twice.
    """
    if name in _tool_versions:
        return _tool_versions[name]
    token = "unknown"
    if name == "qaac":
        checked = find_qaac()
        if checked:
            match = _VERSION_WORD.search(checked) or _VERSION_TOKEN.search(checked)
            if match:
                token = match.group(1) if match.lastindex else match.group(0)
                token = token.rstrip(",.;:")
    elif shutil.which(name) is not None:
        try:
            result = subprocess.run(
                [name, _VERSION_FLAGS.get(name, "--version")],
                capture_output=True, text=True, check=False, timeout=30,
                encoding="utf-8", errors="replace",
            )
        except (subprocess.SubprocessError, OSError):
            result = None
        if result is not None:
            output = (result.stdout or "") + (result.stderr or "")
            first = output.strip().splitlines()[0] if output.strip() else ""
            match = _VERSION_WORD.search(first) or _VERSION_TOKEN.search(first)
            if match:
                token = match.group(1) if match.lastindex else match.group(0)
                token = token.rstrip(",.;:")
    _tool_versions[name] = token
    return token


_TOOLBOX_RE = re.compile(r"CoreAudioToolbox\s+([\d.]+)")


def coreaudio_toolbox_version():
    """CoreAudioToolbox DLL version from `qaac --check`, or None.

    The toolbox (not the qaac frontend) does the actual AAC encoding,
    so its version is what identifies a qaac encode. Returns None when
    qaac is missing or the version cannot be parsed. Never raises.
    """
    checked = find_qaac()
    if not checked:
        return None
    match = _TOOLBOX_RE.search(checked)
    return match.group(1) if match else None


def find_qaac():
    """Verify a genuine qaac binary is usable; return its version line or None.

    Checks PATH plus `qaac --check` output for the qaac/CoreAudioToolbox
    markers, so unrelated same-named executables don't qualify. Result is
    cached for the run (wine startup isn't free).
    """
    global _qaac_version, _qaac_check_result
    if _qaac_version is False:
        return None
    if _qaac_version is not None:
        return _qaac_version
    _qaac_version = False
    _qaac_check_result = None
    if shutil.which("qaac") is None:
        return None
    try:
        result = subprocess.run(
            ["qaac", "--check"],
            capture_output=True, text=True, check=False, timeout=180,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.SubprocessError, OSError):
        return None
    _qaac_check_result = result
    output = (result.stdout or "") + (result.stderr or "")
    first_line = output.strip().splitlines()[0] if output.strip() else ""
    if result.returncode == 0 and first_line.startswith("qaac ") \
            and "CoreAudioToolbox" in output:
        _qaac_version = first_line
        return _qaac_version
    return None


def _apple_dlls_next_to(qaac_path):
    """Apple DLLs present in qaac.exe's own directory (case-insensitive).

    Empty list when the directory is unreadable. Never raises.
    """
    try:
        qdir = os.path.dirname(os.path.abspath(qaac_path))
        lowered = {name.lower() for name in os.listdir(qdir)}
    except Exception:
        return []
    return [dll for dll in _APPLE_DLLS if dll.lower() in lowered]


def qaac_redist_hint():
    """Visual C++ runtime hint for Windows full setup/bundle builds.

    Returns "" unless qaac.exe is present, its `qaac --check` probe
    failed, and the Apple DLLs sit next to it anyway: then the load
    chain is broken at the MSVC runtime level and vc_redist.x64.exe
    is the likely fix. Wording is definitive when the probe died
    with STATUS_DLL_NOT_FOUND, hedged otherwise (no extra spawn:
    find_qaac's cached probe result is reused). Never raises.
    """
    try:
        if sys.platform != "win32":
            return ""
        qpath = _which("qaac")
        if qpath is None or find_qaac() is not None:
            return ""
        if "CoreAudioToolbox.dll" not in _apple_dlls_next_to(qpath):
            return ""
        rc = getattr(_qaac_check_result, "returncode", None)
        if isinstance(rc, int) and (rc & 0xFFFFFFFF) == _DLL_NOT_FOUND:
            return ("Apple DLLs are installed but qaac cannot start "
                    "(missing system DLL): install vc_redist.x64.exe "
                    "(Visual C++ runtime) and re-run --check")
        return ("Apple DLLs are installed but 'qaac --check' still "
                "fails: you may need vc_redist.x64.exe "
                "(Visual C++ runtime)")
    except Exception:
        return ""


def find_lame():
    """True if a genuine lame binary is usable."""
    return _verified_cli("lame", ["--version"], "LAME")


def find_opusenc():
    """True if a genuine opusenc binary is usable."""
    return _verified_cli("opusenc", ["--version"], "opusenc")


def detect_lossy_dialect(fmt, opts):
    """'cli', 'ffmpeg', or None (no recognizable flags) for --lossy-opts.

    Raises ValueError when opts mix both dialects: the flags would only
    work on one backend, so guessing would misroute them.
    """
    cli = any(t in CLI_SHORT_OPTS.get(fmt, ())
              or (t.startswith("--") and len(t) > 2) for t in opts)
    ff = any(t in FFMPEG_ONLY_OPTS
             or re.match(r"^-[A-Za-z]+:a(?::\d+)?$", t) for t in opts)
    if cli and ff:
        native, ffmpeg = _DIALECT_EXAMPLES[fmt]
        raise ValueError(
            f"--lossy-opts mixes {native} and {ffmpeg} flags; "
            f"pick one dialect (one encoder)")
    if cli:
        return "cli"
    if ff:
        return "ffmpeg"
    return None


def select_lossy_backend(fmt, dialect):
    """Pick (and announce) the encoder backend for a lossy format.

    dialect is None (default preference order) or "cli"/"ffmpeg" forced
    by --lossy-opts flag dialect. A forced backend that is unavailable
    fails loudly instead of falling back (its flags wouldn't work
    elsewhere). Quality-fallback warnings only fire on the default path:
    an explicit dialect means the user knows what they chose.
    Returns {"kind": ...} or None after printing an error. Notices go to
    stderr once per run, not per file, for --batch sanity. kind is one of
    "ffmpeg" (in-process codec), "lame", "opusenc" or "qaac".
    """
    forced = " (via --lossy-opts)" if dialect else ""
    if fmt == "mp3":
        if dialect == "cli":
            if not find_lame():
                print("Error: --lossy-opts are lame-style but no lame "
                      "binary found (install lame)", file=sys.stderr)
                return None
            say(f"Encoding MP3 with lame CLI{forced}")
            return {"kind": "lame"}
        if dialect == "ffmpeg":
            if not ffmpeg_has_encoder("libmp3lame"):
                print("Error: --lossy-opts are libmp3lame-style but this "
                      "ffmpeg lacks libmp3lame", file=sys.stderr)
                return None
            say(f"Encoding MP3 with libmp3lame{forced}")
            return {"kind": "ffmpeg"}
        if find_lame():
            say("Encoding MP3 with lame CLI")
            return {"kind": "lame"}
        if ffmpeg_has_encoder("libmp3lame"):
            say("Encoding MP3 with libmp3lame")
            return {"kind": "ffmpeg"}
        print("Error: no MP3 encoder found "
              "(need lame, or ffmpeg with libmp3lame)", file=sys.stderr)
        return None
    if fmt == "opus":
        if dialect == "cli":
            if not find_opusenc():
                print("Error: --lossy-opts are opusenc-style but no "
                      "opusenc binary found (install opus-tools)",
                      file=sys.stderr)
                return None
            say(f"Encoding Opus with opusenc{forced}")
            return {"kind": "opusenc"}
        if dialect == "ffmpeg" and not ffmpeg_has_encoder("libopus"):
            print("Error: --lossy-opts are libopus-style but this "
                  "ffmpeg lacks libopus", file=sys.stderr)
            return None
        if dialect is None and find_opusenc():
            say("Encoding Opus with opusenc")
            return {"kind": "opusenc"}
        if ffmpeg_has_encoder("libopus"):
            if dialect is None:
                print("Warning: opusenc not found, Opus via ffmpeg libopus "
                      "sounds worse (install opus-tools for quality)",
                      file=sys.stderr)
            else:
                say(f"Encoding Opus with ffmpeg libopus{forced}")
            return {"kind": "ffmpeg"}
        if find_opusenc():
            say(f"Encoding Opus with opusenc{forced}")
            return {"kind": "opusenc"}
        print("Error: no Opus encoder found "
              "(need opusenc, or ffmpeg with libopus)", file=sys.stderr)
        return None
    if fmt == "aac":
        version = find_qaac()
        if dialect == "cli":
            if version is None:
                print("Error: --lossy-opts are qaac-style but no qaac "
                      "binary found (install qaac)", file=sys.stderr)
                return None
            say(f"Encoding AAC with {version}{forced}")
            return {"kind": "qaac"}
        if dialect == "ffmpeg" and not ffmpeg_has_encoder("aac"):
            print("Error: --lossy-opts are ffmpeg-style but this "
                  "ffmpeg lacks aac", file=sys.stderr)
            return None
        if dialect is None and version is not None:
            say(f"Encoding AAC with {version}")
            return {"kind": "qaac"}
        if ffmpeg_has_encoder("libfdk_aac"):
            if dialect is None:
                say("Encoding AAC with ffmpeg libfdk_aac")
            else:
                say(f"Encoding AAC with ffmpeg libfdk_aac{forced}")
            return {"kind": "ffmpeg", "encoder": "libfdk_aac"}
        if ffmpeg_has_encoder("aac"):
            if dialect is None:
                say("Encoding AAC with ffmpeg aac")
            else:
                say(f"Encoding AAC with ffmpeg aac{forced}")
            return {"kind": "ffmpeg", "encoder": "aac"}
        if version is not None:
            say(f"Encoding AAC with {version}{forced}")
            return {"kind": "qaac"}
        print("Error: no AAC encoder found "
              "(need qaac, or ffmpeg with aac)", file=sys.stderr)
        return None
    return None


def ffmpeg_has_encoder(name):
    """True if this ffmpeg build includes the named audio encoder."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", f"encoder={name}"],
            capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",
        )
    except OSError:
        return False
    return result.returncode == 0


def run_lame(input_wav, output_mp3, extra_args):
    """Encode MP3 with lame (same lib as libmp3lame); audio only.

    Quality flags come from --lossy-opts (V0 default when absent).
    """
    run_tool(
        ["lame"] + list(extra_args)
        + [str(input_wav), str(output_mp3)],
    )


def run_opusenc(input_wav, output_opus, tags, cover_file, extra_args):
    """Encode Opus with opusenc, fully tagged (ffmpeg can't write opus
    pictures, so tags go here natively, not through a later pass)."""
    # Quality flags come from --lossy-opts (128k default when absent).
    cmd = ["opusenc"] + list(extra_args) + [
        "--discard-comments", "--discard-pictures",
    ]
    for flag, key in (("--title", "title"), ("--artist", "artist"),
                      ("--album", "album"), ("--genre", "genre"),
                      ("--date", "date")):
        if tags.get(key):
            cmd += [flag, tags[key]]
    track = tags.get("track")
    if track and re.fullmatch(r"\d+(/\d+)?", str(track)):
        cmd += ["--tracknumber", str(track).split("/")[0]]
    handled = {"title", "artist", "album", "genre", "date", "track",
               "_extra"}
    for key, value in tags.items():
        if key in handled or not value:
            continue
        cmd += ["--comment", f"{key}={value}"]
    for key, value in tags.get("_extra", {}).items():
        cmd += ["--comment", f"{key}={value}"]
    if cover_file is not None:
        cmd += ["--picture", str(cover_file)]
    cmd += [str(input_wav), str(output_opus)]
    run_tool(cmd)


def run_qaac_alac(input_wav, output_m4a, alac=True, extra_args=()):
    """Encode ALAC (or AAC) with qaac (CoreAudio); audio only.

    input_wav must be integer PCM (qaac rejects float). All metadata and
    cover art goes through ffmpeg afterwards: qaac under wine is a hack
    this script uses strictly as an AAC/ALAC engine, never for tagging.
    extra_args (e.g. TVBR tuning for AAC) go before the output flag.
    Raises CalledProcessError on failure.
    """
    cmd = ["qaac"]
    cmd += ["--alac"] if alac else []
    cmd += list(extra_args)
    cmd += ["--rate", "keep", "-o", str(output_m4a), str(input_wav)]
    run_tool(cmd)


def flac_postprocess(flac_path, image, mime, comment, description, encoder):
    """Fix FLAC metadata ffmpeg gets wrong; warn per file, never raises.

    - image/mime: replace all pictures with this file as Cover (front)
      (ffmpeg never propagates picture types, readers show Other).
    - comment/description: rewrite exactly (ffmpeg merges COMMENT into
      DESCRIPTION on write). None means leave alone.
    - encoder: explicit value via metaflac (ffmpeg stamps its own or,
      under bitexact, nothing at all).
    Reports failure per file with a warning, never raises. (Callers
    pre-check for a metaflac binary and refuse to run when these fixes
    are required but unavailable.)
    """
    calls = []
    if image is not None:
        calls.append(["--remove", "--block-type=PICTURE"])
        calls.append([f"--import-picture-from=3|{mime}|||{image}"])
    tag_ops = []
    if comment is not None or description is not None:
        tag_ops += ["--remove-tag=COMMENT", "--remove-tag=DESCRIPTION"]
    if comment is not None:
        tag_ops.append(f"--set-tag=COMMENT={comment}")
    if description is not None:
        tag_ops.append(f"--set-tag=DESCRIPTION={description}")
    if encoder is not None:
        tag_ops.append(f"--set-tag=ENCODER={encoder}")
    if tag_ops:
        calls.append(tag_ops)
    ok = True
    for op in calls:
        try:
            subprocess.run(["metaflac"] + op + [str(flac_path)],
                           check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            ok = False
    if not ok:
        print(f"Warning: metaflac fixes failed on {flac_path}", file=sys.stderr)
    return ok


def m4a_postprocess(m4a_path, pipeline):
    """Write the encoder_settings freeform atom on an m4a file.

    The mov muxer drops unknown metadata keys (and its use_metadata_tags
    mode breaks attached pictures and tag editors), so the pipeline tag
    goes through mutagen after the encode instead -- the exact analogue
    of the metaflac pass for FLAC. Key stored lowercase, matching every
    other backend. Returns True on success; warns once
    per run and returns False when mutagen is missing (install
    python-mutagen) or the write fails. Never raises.
    """
    global _warned_mutagen
    try:
        from mutagen.mp4 import MP4
    except ImportError:
        MP4 = None
    if MP4 is None:
        if not _warned_mutagen:
            print("Warning: python-mutagen not found (install it); "
                  "skipping the encoder_settings pipeline tag on "
                  f"{m4a_path} (covers and standard tags are unaffected)",
                  file=sys.stderr)
            _warned_mutagen = True
        return False
    try:
        track = MP4(str(m4a_path))
        track["----:com.apple.iTunes:encoder_settings"] = [
            str(pipeline).encode("utf-8")]
        track.save()
        return True
    except Exception as e:
        print(f"Warning: m4a tag fix failed on {m4a_path}: {e}",
              file=sys.stderr)
        return False


_warned_mutagen = False
_warned_mutagen_mp3 = False


def mp3_postprocess(mp3_path, compilation):
    """Write a real TCMP compilation frame on an mp3 file.

    ffmpeg maps `compilation` to a generic TXXX frame, but the
    well-supported convention players expect (iTunes, foobar2000,
    Mp3tag, beets, Picard) is the native TCMP frame -- so it goes
    through mutagen after the encode, mirroring m4a_postprocess.
    Competing TXXX:TCMP/COMPILATION/ITUNESCOMPILATION descriptions
    are removed so readers never see two answers. Returns True on
    success; warns once per run and returns False when mutagen is
    missing (install python-mutagen) or the write fails. Never raises.
    """
    global _warned_mutagen_mp3
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import TCMP
    except ImportError:
        MP3 = None
    if MP3 is None:
        if not _warned_mutagen_mp3:
            print("Warning: python-mutagen not found (install it); "
                  "skipping the TCMP compilation frame on "
                  f"{mp3_path} (other tags are unaffected)",
                  file=sys.stderr)
            _warned_mutagen_mp3 = True
        return False
    try:
        track = MP3(str(mp3_path))
        for key in list(track.keys()):
            if key == "TCMP":
                continue
            if key.startswith("TXXX:") and key.split(":", 1)[1].upper() in (
                    "TCMP", "COMPILATION", "ITUNESCOMPILATION"):
                del track[key]
        track["TCMP"] = TCMP(encoding=3, text=[str(compilation)])
        track.save()
        return True
    except Exception as e:
        print(f"Warning: mp3 tag fix failed on {mp3_path}: {e}",
              file=sys.stderr)
        return False


def missing_metaflac():
    """True (after printing the install error) when metaflac is absent."""
    if shutil.which("metaflac") is None:
        print("Error: metaflac not found (install flac); this FLAC "
              "output needs Cover (front) / comment / encoder fixes",
              file=sys.stderr)
        return True
    return False


def run_ffmpeg(input_path, output_path, audio_filter, codec, title, overwrite,
               metadata_from=None, sample_fmt=None, with_cover=False,
               cover_file=None, strip_tags=False, extra_tags=None,
               ffmpeg_args=None, drop_video=False, drop_tags=None,
               dither=None, bitexact=True):
    """Run one ffmpeg transcode; raises CalledProcessError on failure.

    metadata_from: optional extra input whose global metadata is mapped
    to the output (used to restore tags lost by intermediate sox steps);
    the explicit title always wins. Audio is always taken from input_path.
    sample_fmt: optional forced output depth (s16/s24/s32); None leaves
    the encoder's default negotiation (used for lossless temps/sources).
    with_cover: also map the attached picture + stream tags from
    metadata_from (ffmpeg flac/m4a finals on the sox path).
    cover_file: attach this image as the sole picture instead (drops all
    others); direct encodes then map 0:a explicitly to keep their default
    tag behavior. Combines with metadata_from (cover becomes input 2).
    strip_tags: drop every tag first, then write extra_tags + title
    (tag templates).
    m4a freeform tags (encoder_settings) are finished afterwards by
    m4a_postprocess: the mov muxer drops unknown keys, and its
    use_metadata_tags mode breaks both attached pictures and tag
    editors, so ffmpeg never attempts them.
    drop_tags: tag names (case-insensitive) to exclude while mapping
    (lossy outputs drop stale source encoder tags); implemented as an
    explicit pass-through of everything else.
    dither: integer depth ("s16"/"s24"/"s32") to triangular-dither to
    on int encodes of processed (fractional) audio; None leaves direct
    bit-perfect paths undithered.
    bitexact: suppress the muxer's own encoder/version tags (lossless
    default; lossy outputs keep the encoder's tags unless a template
    curates every tag).
    """
    # movenc (m4a/mp4) recognizes only the exact album_artist metadata
    # key (aART atom); every other spelling is dropped silently, so all
    # metadata bound for those containers goes through movenc_tag_map
    # below. The -map_metadata paths need no remap: their keys come
    # from the demuxers, which already normalize (Vorbis ALBUMARTIST
    # and ID3 TPE2 both surface as album_artist).
    mov_out = str(output_path).lower().endswith((".m4a", ".mp4"))
    ffmpeg_cmd = [
        "ffmpeg",
        "-nostdin",
        "-y" if overwrite else "-n",
        "-i", str(input_path),
    ]
    if metadata_from is not None:
        ffmpeg_cmd += ["-i", str(metadata_from)]
    if cover_file is not None:
        ffmpeg_cmd += ["-i", str(cover_file)]
    if metadata_from is not None:
        ffmpeg_cmd += ["-map", "0:a"]
        if not drop_tags:
            ffmpeg_cmd += ["-map_metadata", "1",
                           "-map_metadata:s:a", "1:s:a"]
        if with_cover:
            ffmpeg_cmd += ["-map", "1:v", "-c:v", "copy",
                           "-disposition:v", "attached_pic"]
        if cover_file is not None:
            # --cover wins outright, even alongside restored metadata
            # (cover input sits at index 2 here, 1 on direct encodes).
            ffmpeg_cmd += ["-map", "2:v",
                           "-c:v", "copy", "-disposition:v", "attached_pic"]
    elif cover_file is not None:
        ffmpeg_cmd += ["-map", "0:a", "-map", "1:v",
                       "-c:v", "copy", "-disposition:v", "attached_pic"]
        if not strip_tags and not drop_tags:
            # (skipped under --tag-template: strip-all already follows and
            # would lose to the merge; explicit tags are added below anyway.
            # Also skipped for drop_tags: the explicit pass-through below
            # starts from a clean slate instead.)
            ffmpeg_cmd += ["-map_metadata", "0", "-map_metadata:s:a", "0:s:a"]
    elif drop_video:
        # Some muxers (ogg-opus) reject video streams outright: audio only.
        ffmpeg_cmd += ["-map", "0:a"]
    if strip_tags or drop_tags:
        ffmpeg_cmd += ["-map_metadata", "-1", "-map_metadata:s:a", "-1"]
    if drop_tags and not strip_tags:
        # Explicit pass-through minus stale tags (a blind -map_metadata
        # would copy them). Source is the metadata input when present,
        # else the audio input itself (ffmpeg's default mapping).
        tag_source = (metadata_from if metadata_from is not None
                      else input_path)
        fmt_tags, stream_tags = read_all_tags(tag_source)
        banned = {t.lower() for t in drop_tags}
        if mov_out:
            # movenc only knows the exact album_artist key: pass-through
            # tags ride in stored case, so every other spelling would be
            # silently dropped from the aART atom.
            fmt_tags = movenc_tag_map(fmt_tags)
            stream_tags = movenc_tag_map(stream_tags)
        for key, value in fmt_tags.items():
            if key.lower() not in banned:
                ffmpeg_cmd += ["-metadata", f"{key}={value}"]
        for key, value in stream_tags.items():
            if key.lower() not in banned:
                ffmpeg_cmd += ["-metadata:s:a", f"{key}={value}"]
    extra = extra_tags or {}
    if mov_out:
        # Same remap for explicit tags (a no-op for template runs --
        # their albumartist already took the movenc alias at the
        # template layer -- but it covers any other spelling, e.g. a
        # literal "Album Artist=" template entry).
        extra = movenc_tag_map(extra)
    for key, value in extra.items():
        ffmpeg_cmd += ["-metadata", f"{key}={value}"]
    audio_filter_chain = audio_filter
    if dither is not None:
        dither_filter = (f"aresample=dither_method=triangular:"
                         f"out_sample_fmt={dither}")
        audio_filter_chain = (f"{audio_filter_chain},{dither_filter}"
                              if audio_filter_chain else dither_filter)
    if audio_filter_chain is not None:
        ffmpeg_cmd += ["-af", audio_filter_chain]
    if sample_fmt is not None:
        ffmpeg_cmd += ["-sample_fmt", sample_fmt]
    ffmpeg_cmd += [
        "-c:a", codec,
    ]
    # Encoder tuning (bitrate/quality defaults, then user --lossy-opts).
    ffmpeg_cmd += list(ffmpeg_args or [])
    ffmpeg_cmd += [
        "-metadata", f"title={title}",
    ]
    if bitexact:
        # Bitexact output: the muxer writes no encoder/version tags of its
        # own, so stripped tags stay stripped (a template `encoder` entry
        # still applies, as do all explicit -metadata values above).
        ffmpeg_cmd += ["-fflags", "+bitexact"]
    ffmpeg_cmd += [str(output_path)]
    run_tool(ffmpeg_cmd)
    if str(output_path).lower().endswith(".m4a"):
        for key, value in (extra_tags or {}).items():
            if str(key).lower() == "encoder_settings" and value:
                m4a_postprocess(output_path, value)
                break
    if str(output_path).lower().endswith(".mp3"):
        for key, value in (extra_tags or {}).items():
            if str(key).lower() == "compilation" and value:
                mp3_postprocess(output_path, value)
                break


def _bundled_dirs():
    """Extra dirs where Windows builds carry their tools.

    The setup/portable tree keeps exes in <install>/tools (on PATH via
    the launcher), the PyInstaller bundle in <_MEIPASS>/tools (on PATH
    via its runtime hook). Both are normally covered by shutil.which
    already; these are belt-and-braces so --check still finds them if
    the PATH patch ever misses. Nonexistent entries are dropped.
    Never raises.
    """
    dirs = []
    try:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(os.path.join(meipass, "tools"))
    except Exception:
        pass
    try:
        exe = sys.executable
        if exe:
            exedir = os.path.dirname(os.path.abspath(exe))
            # Bundle exe dir / setup install root.
            dirs.append(os.path.join(exedir, "tools"))
            dirs.append(exedir)
            # Portable/embedded python: <root>/python/python.exe,
            # tools at <root>/tools.
            dirs.append(os.path.join(
                os.path.dirname(exedir), "tools"))
    except Exception:
        pass
    seen = []
    for d in dirs:
        try:
            if d and d not in seen and os.path.isdir(d):
                seen.append(d)
        except Exception:
            pass
    return seen


def _ensure_bundled_on_path():
    """Prepend existing bundled tool dirs to PATH for this process.

    Makes version probes below see the Windows payload even when the
    launcher hook did not run (Linux: no-op, the dirs don't exist).
    Returns the dirs added. Never raises.
    """
    added = []
    try:
        current = os.environ.get("PATH", "")
        parts = current.split(os.pathsep) if current else []
        for d in _bundled_dirs():
            if d not in parts:
                parts.insert(0, d)
                added.append(d)
        os.environ["PATH"] = os.pathsep.join(parts)
    except Exception:
        pass
    return added


def _which(name):
    """Absolute path of an external tool, or None.

    PATH first, then the Windows bundled-tool dirs (with and without
    .exe). Wrapper around shutil.which so tests can stub presence.
    Never raises.
    """
    try:
        found = shutil.which(name)
    except Exception:
        found = None
    if found:
        return found
    for d in _bundled_dirs():
        for cand in (name, name + ".exe"):
            try:
                p = os.path.join(d, cand)
            except Exception:
                continue
            try:
                if os.path.isfile(p):
                    return p
            except Exception:
                pass
    return None


def _mutagen_version():
    """Installed mutagen version string, or None when missing."""
    try:
        import mutagen
        ver = getattr(mutagen, "version_string", None)
        if ver:
            return str(ver[0] if isinstance(ver, tuple) else ver)
        ver = getattr(mutagen, "version", None)
        if ver:
            return str(ver[0] if isinstance(ver, tuple) else ver)
    except Exception:
        pass
    try:
        from importlib.metadata import version as _dist_version
        return _dist_version("mutagen")
    except Exception:
        return None


def _install_hint(name):
    """Where to get a missing tool, tailored to the running platform."""
    win = sys.platform == "win32"
    hints = {
        "ffmpeg": ("bundled in tools\\: repair/reinstall the setup, "
                   "or drop ffmpeg.exe next to tools\\"
                   if win else
                   "install FFmpeg (provides ffmpeg + ffprobe)"),
        "ffprobe": ("bundled in tools\\: repair/reinstall the setup, "
                    "or drop ffprobe.exe next to tools\\"
                    if win else
                    "install FFmpeg (provides ffmpeg + ffprobe)"),
        "sox": ("bundled in tools\\: repair/reinstall the setup, "
                "or drop sox.exe next to tools\\"
                if win else
                "install SoX (apt/pacman/brew install sox)"),
        "rubberband": ("bundled in tools\\: repair/reinstall the setup, "
                       "or drop rubberband.exe next to tools\\"
                       if win else
                       "install rubberband "
                       "(apt/pacman/brew install rubberband)"),
        "metaflac": ("bundled in tools\\: repair/reinstall the setup, "
                     "or drop metaflac.exe next to tools\\"
                     if win else
                     "install flac (provides metaflac)"),
        "lame": ("bundled in tools\\ (MP3 falls back to ffmpeg "
                 "libmp3lame without it)"
                 if win else
                 "install lame (or rely on ffmpeg libmp3lame)"),
        "opusenc": ("bundled in tools\\ (Opus falls back to ffmpeg "
                    "libopus without it)"
                    if win else
                    "install opus-tools (or rely on ffmpeg libopus)"),
        "opusinfo": ("bundled in tools\\ (Opus tag reads fall back "
                     "to ffprobe without it)"
                     if win else
                     "install opus-tools (provides opusenc + opusinfo)"),
        "vorbiscomment": ("not bundled: Ogg tag reads fall back to "
                          "ffprobe without it"
                          if win else
                          "install vorbis-tools "
                          "(provides vorbiscomment)"),
        "qaac": ("personal builds only: rebuild with --with-qaac, or "
                 "drop qaac.exe + YOUR Apple DLLs (CoreAudioToolbox.dll, "
                 "CoreFoundation.dll, icudt*.dll, libdispatch.dll, "
                 "libicuin*.dll, libicuuc*.dll, objc.dll) into tools\\ "
                 "and re-run --check (ALAC/AAC fall back to ffmpeg "
                 "without it)"
                 if win else
                 "install qaac (qaac-wine on Arch) for best ALAC/AAC "
                 "(else ffmpeg fallback)"),
        "mutagen": ("bundled with the app: reinstall/repair the setup"
                    if win else
                    "pip install mutagen"),
    }
    return hints.get(name, "install it")


# Tools that must be present for full functionality (exit status).
_CHECK_REQUIRED = ("ffmpeg", "ffprobe", "sox", "rubberband",
                   "metaflac", "mutagen")
# Preferred-but-optional native encoders (ffmpeg fallbacks exist).
_CHECK_OPTIONAL_CLI = ("lame", "opusenc", "qaac")
# Exact tag readers (callers fall back to ffprobe without them).
_CHECK_TAG_READERS = ("opusinfo", "vorbiscomment")
# ffmpeg-bundled encoders probed for the readiness footer.
_CHECK_FFMPEG_ENCODERS = ("libmp3lame", "libopus", "libfdk_aac", "aac", "alac",
                          "flac")


def run_check():
    """Print the dependency report (--check) and return an exit code.

    Same code and output on Linux and the Windows setup/bundle builds:
    every external tool plus the python mutagen library, each as
    `name: ok <version> (<path>)` or `name: MISSING (<hint>)`, then
    per-format encode readiness and a one-line verdict. Exit 0 when
    all required tools are present, 1 otherwise (optional tools never
    fail the run). Never raises.
    """
    from . import __full_version__
    _ensure_bundled_on_path()
    plat = f"{sys.platform} {platform.machine()}".strip()
    py_ver = platform.python_version()
    print(f"nightcore {__full_version__} --check "
          f"({plat}, python {py_ver})")

    missing_required = []
    ready_cli = {}
    ffmpeg_present = _which("ffmpeg") is not None

    def _report_simple(name, required):
        path = _which(name)
        if path is None:
            print(f"{name}: MISSING ({_install_hint(name)})")
            if required:
                missing_required.append(name)
            return None
        ver = tool_version(name)
        print(f"{name}: ok {ver} ({path})")
        return path

    # Python runtime itself.
    if sys.version_info < (3, 10):
        print(f"python: BROKEN {py_ver} (needs >= 3.10)")
        missing_required.append("python")
    else:
        print(f"python: ok {py_ver}")

    # Python library (no exe path to show).
    mver = _mutagen_version()
    if mver is None:
        print(f"mutagen: MISSING ({_install_hint('mutagen')})")
        missing_required.append("mutagen")
    else:
        print(f"mutagen: ok {mver}")

    for name in ("ffmpeg", "ffprobe", "sox", "rubberband",
                 "metaflac"):
        _report_simple(name, required=True)

    for name in _CHECK_OPTIONAL_CLI:
        if name == "qaac":
            continue
        verifier = find_lame if name == "lame" else find_opusenc
        try:
            ok = bool(verifier())
        except Exception:
            ok = False
        path = _which(name)
        ready_cli[name] = ok
        if ok:
            print(f"{name}: ok {tool_version(name)} ({path})")
        elif path is not None:
            print(f"{name}: BROKEN (found at {path} but failed "
                  f"verification; {_install_hint(name)})")
        else:
            print(f"{name}: MISSING ({_install_hint(name)}) "
                  f"(optional)")

    # qaac: a present-but-unusable binary (missing Apple DLLs) is the
    # common Windows case, so distinguish it from truly absent.
    qpath = _which("qaac")
    try:
        qline = find_qaac()
    except Exception:
        qline = None
    ready_cli["qaac"] = bool(qline)
    if qline:
        try:
            toolbox = coreaudio_toolbox_version()
        except Exception:
            toolbox = None
        extra = (f"; CoreAudioToolbox {toolbox}"
                 if toolbox else "")
        print(f"qaac: ok {tool_version('qaac')} ({qpath}{extra})")
    elif qpath is not None:
        redist = qaac_redist_hint()
        reason = redist if redist else \
            f"missing Apple DLLs? {_install_hint('qaac')}"
        print(f"qaac: BROKEN (found at {qpath} but 'qaac --check' "
              f"failed: {reason}) "
              f"(optional)")
    else:
        print(f"qaac: MISSING ({_install_hint('qaac')}) (optional)")

    for name in _CHECK_TAG_READERS:
        path = _which(name)
        if path is None:
            print(f"{name}: MISSING ({_install_hint(name)}) "
                  f"(optional)")
        else:
            print(f"{name}: ok {tool_version(name)} ({path}) "
                  f"(optional)")

    # ffmpeg-bundled encoders (informational; unknown without ffmpeg).
    enc_have = {}
    for enc in _CHECK_FFMPEG_ENCODERS:
        if not ffmpeg_present:
            print(f"ffmpeg {enc}: unknown (no ffmpeg)")
            enc_have[enc] = False
            continue
        try:
            have = bool(ffmpeg_has_encoder(enc))
        except Exception:
            have = False
        enc_have[enc] = have
        print(f"ffmpeg {enc}: {'yes' if have else 'no'}")

    def _ready_line(fmt, via_cli, via_cli_name, via_ff, ff_name):
        if via_cli and via_ff:
            return (f"{fmt} encode: ready via {via_cli_name} "
                    f"(ffmpeg {ff_name} fallback ok)")
        if via_cli:
            return f"{fmt} encode: ready via {via_cli_name}"
        if via_ff:
            return f"{fmt} encode: ready via ffmpeg {ff_name}"
        return (f"{fmt} encode: UNAVAILABLE (no {via_cli_name} and "
                f"no ffmpeg {ff_name})")

    print(_ready_line("mp3", ready_cli.get("lame"),
                      "lame", enc_have.get("libmp3lame"),
                      "libmp3lame"))
    print(_ready_line("opus", ready_cli.get("opusenc"),
                      "opusenc", enc_have.get("libopus"),
                      "libopus"))
    aac_ff = "libfdk_aac" if enc_have.get("libfdk_aac") else "aac"
    print(_ready_line("aac", ready_cli.get("qaac"),
                      "qaac", enc_have.get("aac") or enc_have.get("libfdk_aac"),
                      aac_ff))
    print(_ready_line("alac", ready_cli.get("qaac"),
                      "qaac", enc_have.get("alac"), "alac"))
    print(f"flac encode: "
          f"{'ready via ffmpeg flac' if enc_have.get('flac')
              else 'UNAVAILABLE (no ffmpeg flac)'}")

    if missing_required:
        print(f"check: MISSING required tools: "
              f"{', '.join(missing_required)}")
        return 1
    print("check: all required tools present")
    return 0
