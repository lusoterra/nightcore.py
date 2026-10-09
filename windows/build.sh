#!/usr/bin/env bash
# build.sh — all-in-one Linux builder for nightcore Windows outputs.
#   1. resolves app source from Gitea (or $NIGHTCORE_SRC) into build/
#   2. fetches Windows tools into tools/ (cached in cache/ after first run)
#   3. assembles the portable tree in build/portable/
#   4. builds the NSIS setup exe(s) into dist/
#   5. builds the PyInstaller bundle exe(s) under Wine into dist/
#
# Usage:
#   ./build.sh                                      # portable + setup + bundle (shareable-safe)
#   ./build.sh --fetch-only                         # only fill tools/ + cache/, then stop
#   ./build.sh --no-download                        # stage from cache/tools only, no network
#   ./build.sh --skip-setup | --skip-bundle         # only one installer type
#   ./build.sh --portable-only                      # stop after build/portable
#   ./build.sh --with-qaac | --full | --build-all   # qaac variants (setup + bundle)
#
# Qaac modes (apply to setup + bundle):
#   (default)   no qaac / Apple DLLs, even if staged   (-setup.exe / -bundle.exe)
#   --with-qaac + qaac.exe, NO Apple DLLs              (-setup-qaac.exe, drag-drop yours)
#   --full      + qaac + your Apple DLLs (personal)    (-setup-full.exe, do not share)
#   --build-all all three
#
# Nothing is ever deleted for you: if an output dir already exists the
# script stops and tells you the exact `rm -rf` to run yourself.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

STAGE="tools"
CACHE="cache"
PORTABLE="build/portable"
UPSTREAM_REPO="https://git.lusosterone.net/lev/nightcore.py"

# all bumpable/pinnable versions live in versions.env (env still wins).
. "$ROOT/versions.env"

# derived locations/specs — override the _URL vars in versions.env
# (or the environment) to point at mirrors.
WINE_PY_TAG="$(printf '%s' "$WINE_PY_VER" | awk -F. '{print $1$2}')"
WINE_PY_URL="https://www.python.org/ftp/python/${WINE_PY_VER}/python-${WINE_PY_VER}-amd64.exe"
SOX_URL="${SOX_URL:-https://sourceforge.net/projects/sox/files/sox/${SOX_VER}/sox-${SOX_VER}-win32.zip/download}"
RUBBERBAND_URL="${RUBBERBAND_URL:-https://breakfastquay.com/files/releases/rubberband-${RUBBERBAND_VER}-gpl-executable-windows.zip}"
LAME_URL="${LAME_URL:-https://www.rarewares.org/files/mp3/lame${LAME_VER}-x64.zip}"
OPUS_URL="${OPUS_URL:-https://www.rarewares.org/files/opus/opus-tools-${OPUS_VER}(using%20libopus%20${LIBOPUS_VER})-x64.zip}"
OPUS_FALLBACK_URL="${OPUS_FALLBACK_URL:-https://www.rarewares.org/files/opus/${OPUS_FALLBACK_ZIP}}"
FLAC_URL="${FLAC_URL:-https://www.rarewares.org/files/lossless/flac-${FLAC_VER}-x64.zip}"
VORBIS_URL="${VORBIS_URL:-http://www.rarewares.org/files/ogg/vorbiscomment-${VORBIS_VER}.zip}"
if [ -n "$MUTAGEN_VER" ]; then MUTAGEN_SPEC="mutagen==$MUTAGEN_VER"; else MUTAGEN_SPEC="mutagen"; fi
if [ -n "$PYINSTALLER_VER" ]; then PYINSTALLER_SPEC="pyinstaller==$PYINSTALLER_VER"; else PYINSTALLER_SPEC="pyinstaller"; fi

# fix: these must exist BEFORE any curl -o cache/... or tar -xzf
# (the old split scripts sourced lib-upstream.sh before mkdir, so the
# first run died with `curl: (23) ... cache/nightcore-src-*.tar.gz:
# No such file or directory`).
mkdir -p "$STAGE" "$CACHE" build dist

FETCH_ONLY=0; NO_DOWNLOAD=0; SKIP_SETUP=0; SKIP_BUNDLE=0; PORTABLE_ONLY=0
WITH_QAAC=0; FULL=0; BUILD_ALL=0
for a in "$@"; do case "$a" in
    --fetch-only) FETCH_ONLY=1;;
    --no-download) NO_DOWNLOAD=1;;
    --skip-setup) SKIP_SETUP=1;;
    --skip-bundle) SKIP_BUNDLE=1;;
    --portable-only) PORTABLE_ONLY=1;;
    --with-qaac) WITH_QAAC=1;;
    --full) FULL=1;;
    --build-all) BUILD_ALL=1;;
    *) echo "unknown flag: $a" >&2; exit 2;;
esac; done
if [ "$WITH_QAAC" -eq 1 ] && [ "$FULL" -eq 1 ]; then
    echo "pick one of --with-qaac or --full (or --build-all)" >&2; exit 2
fi
if [ "$PORTABLE_ONLY" -eq 1 ]; then SKIP_SETUP=1; SKIP_BUNDLE=1; fi

# ---- upstream: app source (sets SRC) ----
resolve_upstream() {
    if [ -n "${NIGHTCORE_SRC:-}" ]; then
        SRC="$NIGHTCORE_SRC"
    else
        mkdir -p "$CACHE" build
        if [ -n "$NIGHTCORE_VERSION" ]; then
            UP_TAG="$NIGHTCORE_VERSION"
            echo "upstream: $UP_TAG (pinned via NIGHTCORE_VERSION)" >&2
        else
            UP_TAG="$(git ls-remote "$UPSTREAM_REPO" 2>/dev/null \
                | grep -oE 'refs/tags/v[0-9][^ ]*' | sed 's|refs/tags/||' | sort -uV | tail -n 1)"
            [ -n "${UP_TAG:-}" ] || { echo "cannot list tags at $UPSTREAM_REPO" >&2; exit 1; }
            echo "upstream: $UP_TAG (latest)" >&2
        fi
        UP_TARBALL="$CACHE/nightcore-src-$UP_TAG.tar.gz"
        if [ ! -s "$UP_TARBALL" ]; then
            echo "GET $UPSTREAM_REPO/archive/$UP_TAG.tar.gz"
            curl -fL --retry 3 -o "$UP_TARBALL" "$UPSTREAM_REPO/archive/$UP_TAG.tar.gz"
        else
            echo "cached: $UP_TARBALL"
        fi
        SRC="build/upstream-$UP_TAG"
        if [ ! -d "$SRC/nightcore" ]; then
            if ls -d build/upstream-* >/dev/null 2>&1; then
                echo "refusing to overwrite existing build/upstream-* (wanted $SRC)" >&2
                echo "delete it yourself first:" >&2
                echo "  rm -rf \"$ROOT/build/upstream-\"*" >&2
                exit 1
            fi
            mkdir -p "$SRC"
            tar -xzf "$UP_TARBALL" -C "$SRC" --strip-components=1
        fi
    fi
    [ -d "$SRC/nightcore" ] || { echo "nightcore source missing: $SRC" >&2; exit 1; }
}

dl() { # dl URL OUTFILE (skip if present)
    local url="$1" out="$2"
    mkdir -p "$(dirname "$out")"
    [ -s "$out" ] && { echo "cached: $out"; return 0; }
    echo "GET $url"; curl -fL --retry 3 -o "$out" "$url"
}

pick_exe() { # pick_exe ZIP PATTERN DESTNAME
    local zip="$1" pat="$2" dest="$3" tmp
    tmp="$(mktemp -d)"
    7z x -y -o"$tmp" "$zip" >/dev/null
    local hit; hit="$(find "$tmp" -iname "$pat" -type f | head -n 1)"
    [ -n "$hit" ] || { rm -rf "$tmp"; echo "!! $pat not in $zip" >&2; return 1; }
    cp -f "$hit" "$STAGE/$dest"; echo "staged: $dest"
    rm -rf "$tmp"
}

fetch_tools() {
    echo "== fetch =="
    dl "https://www.python.org/ftp/python/$PY_VER/python-$PY_VER-embed-amd64.zip" \
        "$CACHE/python-embed.zip"
    dl "$FFMPEG_URL" \
        "$CACHE/ffmpeg.zip" || echo "!! ffmpeg fetch failed (drop ffmpeg.exe/ffprobe.exe in $STAGE/)"
    dl "$SOX_URL" \
        "$CACHE/sox.zip" || echo "!! sox fetch failed"
    dl "$RUBBERBAND_URL" \
        "$CACHE/rubberband.zip" || echo "!! rubberband fetch failed"
    dl "$LAME_URL" \
        "$CACHE/lame.zip" || echo "!! lame fetch failed (mp3 falls back to ffmpeg libmp3lame)"
    dl "$OPUS_URL" \
        "$CACHE/opus.zip" || \
        dl "$OPUS_FALLBACK_URL" \
        "$CACHE/opus.zip" || echo "!! opus fetch failed (opus falls back to ffmpeg libopus)"
    dl "$FLAC_URL" \
        "$CACHE/flac.zip" || echo "!! flac fetch failed (metaflac needed for FLAC covers)"
    dl "$VORBIS_URL" \
        "$CACHE/vorbis.zip" || echo "!! vorbiscomment fetch failed (Ogg tag reads fall back to ffprobe)"

    [ -s "$CACHE/ffmpeg.zip" ] && { pick_exe "$CACHE/ffmpeg.zip" "ffmpeg.exe" "ffmpeg.exe" || true
        pick_exe "$CACHE/ffmpeg.zip" "ffprobe.exe" "ffprobe.exe" || true; }
    [ -s "$CACHE/sox.zip" ] && { tmpd="$(mktemp -d)"; 7z x -y -o"$tmpd" "$CACHE/sox.zip" >/dev/null
        find "$tmpd" -iname "*.exe" -not -iname "wget.exe" -o -iname "*.dll" | while read -r f; do cp -f "$f" "$STAGE/"; echo "staged: $(basename "$f")"; done; rm -rf "$tmpd"; }
    [ -s "$CACHE/rubberband.zip" ] && { tmpd="$(mktemp -d)"; 7z x -y -o"$tmpd" "$CACHE/rubberband.zip" >/dev/null
        find "$tmpd" -iname "*.exe" -o -iname "*.dll" | while read -r f; do cp -f "$f" "$STAGE/"; echo "staged: $(basename "$f")"; done; rm -rf "$tmpd"; }
    [ -s "$CACHE/lame.zip" ] && { tmpd="$(mktemp -d)"; 7z x -y -o"$tmpd" "$CACHE/lame.zip" >/dev/null
        find "$tmpd" -iname "lame*.exe" | head -n 1 | while read -r f; do cp -f "$f" "$STAGE/lame.exe"; echo "staged: lame.exe"; done; rm -rf "$tmpd"; } || true
    [ -s "$CACHE/opus.zip" ] && { tmpd="$(mktemp -d)"; 7z x -y -o"$tmpd" "$CACHE/opus.zip" >/dev/null
        find "$tmpd" -iname "opusenc.exe" | head -n 1 | while read -r f; do cp -f "$f" "$STAGE/"; echo "staged: opusenc.exe"; done
        find "$tmpd" -iname "opusinfo.exe" | head -n 1 | while read -r f; do cp -f "$f" "$STAGE/"; echo "staged: opusinfo.exe"; done; rm -rf "$tmpd"; } || true
    [ -s "$CACHE/flac.zip" ] && { tmpd="$(mktemp -d)"; 7z x -y -o"$tmpd" "$CACHE/flac.zip" >/dev/null
        find "$tmpd" -iname "metaflac.exe" | head -n 1 | while read -r f; do cp -f "$f" "$STAGE/"; echo "staged: metaflac.exe"; done; rm -rf "$tmpd"; } || true
    [ -s "$CACHE/vorbis.zip" ] && { tmpd="$(mktemp -d)"; 7z x -y -o"$tmpd" "$CACHE/vorbis.zip" >/dev/null
        find "$tmpd" -iname "vorbiscomment.exe" | head -n 1 | while read -r f; do cp -f "$f" "$STAGE/"; echo "staged: vorbiscomment.exe"; done; rm -rf "$tmpd"; } || true
    # mutagen is pure-python: one wheel works for the Win embed env too
    if ! ls "$CACHE"/mutagen-*.whl >/dev/null 2>&1; then
        pip3 download "$MUTAGEN_SPEC" -d "$CACHE" --only-binary=:all: --no-deps || echo "!! mutagen fetch failed"
    fi
}

assemble_portable() {
    echo "== assemble $PORTABLE =="
    if [ -e "$PORTABLE" ]; then
        echo "refusing to overwrite existing $PORTABLE" >&2
        echo "delete it yourself first:" >&2
        echo "  rm -rf \"$ROOT/$PORTABLE\"" >&2
        exit 1
    fi
    mkdir -p "$PORTABLE/python" "$PORTABLE/app" "$PORTABLE/tools"

    # 1. windows python embed
    if [ -s "$CACHE/python-embed.zip" ]; then
        unzip -q -o "$CACHE/python-embed.zip" -d "$PORTABLE/python"
        # allow site + our app dir: _pth name varies (python312._pth)
        PTH="$(ls "$PORTABLE/python"/python*._pth | head -n 1)"
        { echo "../app"; echo "./Lib/site-packages"; grep -v "^#" "$PTH" | grep -v "^\.\./app" || true; echo "import site"; } | awk '!seen[$0]++' > "$PTH.new"; mv "$PTH.new" "$PTH"
        mkdir -p "$PORTABLE/python/Lib/site-packages"
        WHEEL="$(ls -t "$CACHE"/mutagen-*.whl 2>/dev/null | head -n 1 || true)"
        if [ -n "$WHEEL" ]; then unzip -q -o "$WHEEL" -d "$PORTABLE/python/Lib/site-packages"; echo "mutagen from $(basename "$WHEEL")"
        else echo "!! no mutagen wheel in $CACHE (m4a/mp3 postprocess will warn)" >&2; fi
    else
        echo "!! $CACHE/python-embed.zip missing (need network once)" >&2; exit 1
    fi

    # 2. nightcore source (no __pycache__: Linux .pyc files are useless on Win)
    mkdir -p "$PORTABLE/app/nightcore"
    tar --exclude='__pycache__' -cf - -C "$SRC" nightcore | tar -xf - -C "$PORTABLE/app"

    # 3. tools: everything staged (filtering happens per-output later).
    for f in "$STAGE"/*; do [ -f "$f" ] || continue
        b="$(basename "$f")"
        case "$b" in qaac64.exe) b="qaac.exe";; refalac64.exe) b="refalac.exe";; esac
        cp -f "$f" "$PORTABLE/tools/$b"
    done
    cp -f nightcore.bat "$PORTABLE/"
    cp -f "$SRC/pyproject.toml" "$PORTABLE/app/pyproject.toml" 2>/dev/null || true

    echo "--- staged contents ---"
    ls -la "$PORTABLE" "$PORTABLE/tools" | head -n 60
    du -sh "$PORTABLE"
    [ -f "$PORTABLE/tools/ffmpeg.exe" ] || echo "!! no ffmpeg.exe (core requires it)" >&2
    [ -f "$PORTABLE/tools/qaac.exe" ] || echo "!! no qaac.exe (ALAC/AAC falls back to ffmpeg)" >&2
    [ -f "$PORTABLE/tools/metaflac.exe" ] || echo "!! no metaflac.exe (FLAC cover/COMMENT fixes will fail)" >&2
}

APPLE="ASL.dll CoreAudioToolbox.dll CoreFoundation.dll icudt62.dll libdispatch.dll libicuin.dll libicuuc.dll objc.dll"
QAAC="qaac.exe refalac.exe"

build_setup_one() { # build_setup_one MODE (default|qaac|full)
    local mode="$1" tag="" payload
    case "$mode" in
        default) tag="";;
        qaac) tag="-qaac";;
        full) tag="-full";;
        *) echo "bad setup mode: $mode" >&2; exit 2;;
    esac
    # per-variant payload dir so --build-all never overwrites itself
    if [ -z "$tag" ]; then payload="build/payload-setup";
    else payload="build/payload-setup$tag"; fi
    echo "== setup payload ($mode) -> $payload =="
    if [ -e "$payload" ]; then
        echo "refusing to overwrite existing $payload" >&2
        echo "delete it yourself first:" >&2
        echo "  rm -rf \"$ROOT/$payload\"" >&2
        exit 1
    fi

    NSIS="cache/nsis"
    if [ ! -x "$NSIS/a/usr/bin/makensis" ]; then
        echo "== fetch makensis =="
        mkdir -p "$NSIS/a" "$NSIS/b"
        curl -fL --retry 3 -o "$NSIS/nsis.deb" \
            "https://ftp.debian.org/debian/pool/main/n/nsis/nsis_${NSIS_VER}_amd64.deb"
        curl -fL --retry 3 -o "$NSIS/nsis-common.deb" \
            "https://ftp.debian.org/debian/pool/main/n/nsis/nsis-common_${NSIS_VER}_all.deb"
        bsdtar -xf "$NSIS/nsis.deb" -C "$NSIS/a"
        bsdtar -xf "$NSIS/nsis-common.deb" -C "$NSIS/b"
        bsdtar -xf "$NSIS/a/data.tar.xz" -C "$NSIS/a"
        bsdtar -xf "$NSIS/b/data.tar.xz" -C "$NSIS/b"
    fi
    cp -rn "$NSIS/b/usr/share/nsis/." "$NSIS/a/usr/share/nsis/" 2>/dev/null || true

    cp -a build/portable "$payload"
    if [ "$mode" = full ]; then
        # top up from staging (personal Apple DLLs live in tools/)
        for f in $QAAC $APPLE; do
            [ -f "$payload/tools/$f" ] \
                || cp -f "tools/$f" "$payload/tools/$f" 2>/dev/null || true
        done
        echo "qaac + Apple DLLs included (personal installer, do not redistribute)"
    elif [ "$mode" = qaac ]; then
        for f in $QAAC; do
            [ -f "$payload/tools/$f" ] \
                || cp -f "tools/$f" "$payload/tools/$f" 2>/dev/null || true
        done
        for f in $APPLE; do rm -f "$payload/tools/$f"; done
        echo "qaac included, Apple DLLs excluded (drop your own into tools\\)"
    else
        rm -f "$payload/tools/qaac.exe" "$payload/tools/refalac.exe"
        for f in $APPLE; do rm -f "$payload/tools/$f"; done
        echo "qaac + Apple DLLs excluded (pass --with-qaac, or --full for personal)"
    fi
    du -sh "$payload"
    echo "qaac in setup: $(ls "$payload/tools/qaac.exe" 2>/dev/null || echo NO)"

    mkdir -p build dist
    sed -e "s|@VER@|$VER|g" -e "s|@ROOT@|$ROOT|g" -e "s|@TAG@|$tag|g" \
        -e "s|@PAYLOAD@|$(basename "$payload")|g" \
        -e "s|@OUT@|nightcore-$VER-win64-setup$tag.exe|g" \
        installer-setup.nsi > build/"installer-setup$tag.nsi"

    echo "== makensis (user+mount namespace) =="
    STUBS="$ROOT/$NSIS/a/usr/share/nsis"
    MAKENSIS="$ROOT/$NSIS/a/usr/bin/makensis"
    unshare -Urm bash -c '
        mount -t tmpfs tmpfs /usr/share &&
        mkdir -p /usr/share/nsis &&
        mount --bind "$0" /usr/share/nsis &&
        exec "$1" -V2 "$2"' "$STUBS" "$MAKENSIS" "build/installer-setup$tag.nsi"
    ls -la dist/
    file "dist/nightcore-$VER-win64-setup$tag.exe"
}

build_bundle_one() { # build_bundle_one MODE (default|qaac|full)
    local mode="$1" tag=""
    case "$mode" in
        default) tag="";;
        qaac) tag="-qaac";;
        full) tag="-full";;
        *) echo "bad bundle mode: $mode" >&2; exit 2;;
    esac
    echo "== bundle ($mode) =="
    export WINEDEBUG=-all
    # NOTE: WINEPREFIX is an isolated mktemp dir (NOT ${WINEPREFIX}), because
    # this shell pre-exports WINEPREFIX (default Wine prefix) and a
    # ${WINEPREFIX:-...} default would silently use it. Never write to the
    # default prefix (~/.wine, ~/.local/share/wine).
    local prefix; prefix="$(mktemp -d -t nightcore-wine.XXXXXX)"
    export WINEPREFIX="$prefix"
    echo "wine prefix: $WINEPREFIX"
    echo "cleanup when done: rm -rf \"$WINEPREFIX\""

    find_wine_python() {
        for p in "$WINEPREFIX/drive_c/Python${WINE_PY_TAG}/python.exe" \
            "$WINEPREFIX/drive_c/users/$USER/AppData/Local/Programs/Python/Python${WINE_PY_TAG}/python.exe"; do
            [ -x "$p" ] && { echo "$p"; return 0; }
        done
        return 1
    }
    local pyexe=""
    if ! pyexe="$(find_wine_python)"; then
        echo "== install windows python in isolated prefix $WINEPREFIX =="
        local pysetup="cache/python-amd64.exe"
        mkdir -p cache
        [ -s "$pysetup" ] || curl -fL --retry 3 -o "$pysetup" \
            "$WINE_PY_URL"
        timeout 600 wine "$pysetup" /quiet InstallAllUsers=0 PrependPath=0 Include_test=0
        pyexe="$(find_wine_python)" || { echo "python install failed" >&2; exit 1; }
    fi
    timeout 120 wine "$pyexe" --version

    echo "== pip deps =="
    timeout 300 wine "$pyexe" -m pip install --upgrade pip
    timeout 600 wine "$pyexe" -m pip install "$PYINSTALLER_SPEC" "$MUTAGEN_SPEC"

    echo "== stage inputs on C: =="
    local b="$WINEPREFIX/drive_c/build"
    if [ -e "$b" ]; then
        echo "refusing to overwrite existing $b" >&2
        echo "delete it yourself first:" >&2
        echo "  rm -rf \"$b\"" >&2
        exit 1
    fi
    mkdir -p "$b/src" "$b/tools"
    cp -r "$SRC/nightcore" "$b/src/nightcore"
    cp -f "$SRC/pyproject.toml" "$b/src/pyproject.toml"
    cp -f rthook.py "$b/rthook.py"
    cp -f entry-bundle.py "$b/entry-bundle.py"
    # tools: everything staged, minus personal files unless flagged
    cp -f tools/* "$b/tools/" 2>/dev/null || true
    if [ -f "$b/tools/qaac64.exe" ]; then mv "$b/tools/qaac64.exe" "$b/tools/qaac.exe"; fi
    if [ -f "$b/tools/refalac64.exe" ]; then mv "$b/tools/refalac64.exe" "$b/tools/refalac.exe"; fi
    if [ "$mode" = full ]; then
        echo "qaac + Apple DLLs included (personal bundle, do not redistribute)"
    elif [ "$mode" = qaac ]; then
        for d in $APPLE; do rm -f "$b/tools/$d"; done
        echo "qaac included, Apple DLLs excluded (drop your own into tools\\)"
    else
        rm -f "$b/tools/qaac.exe" "$b/tools/refalac.exe"
        for d in $APPLE; do rm -f "$b/tools/$d"; done
        echo "qaac + Apple DLLs excluded (pass --with-qaac, or --full for personal)"
    fi

    echo "== verify staged inputs =="
    ls -la "$b/src" | head -n 20
    ls "$b/tools" | head -n 40
    [ -f "$b/src/pyproject.toml" ] || { echo "staging failed: $b/src/pyproject.toml missing" >&2; exit 1; }
    [ -d "$b/src/nightcore" ] || { echo "staging failed: $b/src/nightcore missing" >&2; exit 1; }

    # install nightcore itself so importlib.metadata finds a version at runtime
    # (frozen modules can't see pyproject.toml); spec collects the dist-info.
    # NOTE: forward slashes — pip's requirement parser rejects backslash
    # paths ("Invalid requirement: 'C:\\build\\src'"), forward slashes work.
    timeout 300 wine "$pyexe" -m pip install "C:/build/src"

    echo "== write spec (windows paths) =="
    local binlist; binlist="$(ls "$b/tools" | sed 's/.*/\("C:\\\\build\\\\tools\\\\&", "tools"\),/' | tr '\n' ' ')"
    cat > "$b/bundle.spec" <<EOF
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import copy_metadata
a = Analysis(
    ["C:\\\\build\\\\entry-bundle.py"],
    pathex=["C:\\\\build\\\\src"],
    binaries=[ $binlist ],
    datas=copy_metadata("nightcore"),
    hiddenimports=["mutagen", "mutagen.mp4", "mutagen.mp3", "mutagen.id3",
                   "mutagen.flac", "mutagen.oggopus", "mutagen.oggvorbis"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["C:\\\\build\\\\rthook.py"],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="nightcore-bundle",
    debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True,
    disable_windowed_traceback=False, argv_emulation=False,
)
EOF

    echo "== pyinstaller =="
    mkdir -p dist
    cd "$b"
    timeout 1200 wine "$pyexe" -m PyInstaller --noconfirm --clean \
        --distpath "C:\\build\\dist" --workpath "C:\\build\\work" bundle.spec
    ls -la "$b/dist/"
    cp -f "$b/dist/nightcore-bundle.exe" "$ROOT/dist/nightcore-$VER-win64-bundle$tag.exe"
    cd "$ROOT"
    ls -la "dist/nightcore-$VER-win64-bundle$tag.exe"
    echo "wine prefix left at $WINEPREFIX — delete it yourself:" >&2
    echo "  rm -rf \"$WINEPREFIX\"" >&2
}

# ---- main ----
resolve_upstream
echo "versions: nightcore=${NIGHTCORE_VERSION:-latest} py=$PY_VER wine-py=$WINE_PY_VER"
echo "versions: sox=$SOX_VER rubberband=$RUBBERBAND_VER lame=$LAME_VER opus=$OPUS_VER libopus=$LIBOPUS_VER"
echo "versions: flac=$FLAC_VER vorbis=$VORBIS_VER mutagen=${MUTAGEN_VER:-latest} pyinstaller=${PYINSTALLER_VER:-latest} nsis=$NSIS_VER"
if [ "$NO_DOWNLOAD" -eq 0 ]; then
    fetch_tools
else
    echo "(--no-download: skipping fetch, using cache/tools as-is)"
fi
[ "$FETCH_ONLY" -eq 1 ] && { echo "fetch done."; exit 0; }

assemble_portable
VER="$(grep -m1 '^version' "$SRC/pyproject.toml" | sed 's/.*"\([^"]*\)".*/\1/')"

MODES="default"
if [ "$BUILD_ALL" -eq 1 ]; then MODES="default qaac full"
elif [ "$WITH_QAAC" -eq 1 ]; then MODES="qaac"
elif [ "$FULL" -eq 1 ]; then MODES="full"
fi

if [ "$SKIP_SETUP" -eq 0 ]; then
    [ -d build/portable/tools ] || { echo "portable tools missing" >&2; exit 1; }
    for m in $MODES; do build_setup_one "$m"; done
fi
if [ "$SKIP_BUNDLE" -eq 0 ]; then
    for m in $MODES; do build_bundle_one "$m"; done
fi
echo "done. outputs in $ROOT/dist + $ROOT/build/portable"
