#!/usr/bin/env bash
# build.sh — all-in-one Linux builder for the nightcore Windows setup.
#   1. resolves app source from Gitea (or $NIGHTCORE_SRC) into build/
#   2. fetches Windows tools into tools/ (cached in cache/ after first run)
#   3. assembles the portable tree in build/portable/
#   4. builds the NSIS setup exe(s) into dist/
#
# Usage:
#   ./build.sh                                      # portable + setup (shareable-safe)
#   ./build.sh --fetch-only                         # only fill tools/ + cache/, then stop
#   ./build.sh --no-download                        # stage from cache/tools only, no network
#   ./build.sh --portable-only                      # stop after build/portable
#   ./build.sh --with-qaac | --full | --build-all   # qaac variants (setup)
#   ./build.sh --no-upx                             # skip UPX packing (exes may exceed 100MB)
#
# Qaac modes (apply to setup):
#   (default)   no qaac / Apple DLLs, even if staged   (-setup.exe)
#   --with-qaac + qaac.exe, NO Apple DLLs              (-setup-qaac.exe, drag-drop yours)
#   --full      + qaac + your Apple DLLs (personal)    (-setup-full.exe, do not share)
#   --build-all all three
#
# Stale build outputs (build/portable, build/payload-*, build/upstream-*)
# are removed automatically on the way through. cache/ and tools/ are
# never touched, and nothing outside build/ is ever deleted.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

STAGE="tools"
CACHE="cache"
PORTABLE="build/portable"
UPSTREAM_REPO="https://github.com/lusoterra/nightcore.py"

# all bumpable/pinnable versions live in versions.env (env still wins).
. "$ROOT/versions.env"

# derived locations/specs — override the _URL vars in versions.env
# (or the environment) to point at mirrors.
# Windows target is fixed x86_64 (amd64 artifacts, x86_64 dist names).
# Both python embed zips are headless-safe; the Burn installer .exe is
# deliberately not used (it fails with "Failed to create window",
# exit 0x582, when no display server runs).
PY_EMBED_URL="https://www.python.org/ftp/python/${PY_VER}/python-${PY_VER}-embed-amd64.zip"
# NSIS .debs run on THIS (Linux host) machine, not the Windows target.
NSIS_HOST_ARCH="$(dpkg --print-architecture 2>/dev/null || true)"
if [ -z "$NSIS_HOST_ARCH" ]; then
    case "$(uname -m)" in
        x86_64) NSIS_HOST_ARCH="amd64";;
        aarch64|arm64) NSIS_HOST_ARCH="arm64";;
        *) echo "unsupported host arch: $(uname -m)" >&2; exit 2;;
    esac
fi
SOX_URL="${SOX_URL:-https://sourceforge.net/projects/sox/files/sox/${SOX_VER}/sox-${SOX_VER}-win32.zip/download}"
RUBBERBAND_URL="${RUBBERBAND_URL:-https://breakfastquay.com/files/releases/rubberband-${RUBBERBAND_VER}-gpl-executable-windows.zip}"
LAME_URL="${LAME_URL:-https://www.rarewares.org/files/mp3/lame${LAME_VER}-x64.zip}"
OPUS_URL="${OPUS_URL:-https://www.rarewares.org/files/opus/opus-tools-${OPUS_VER}(using%20libopus%20${LIBOPUS_VER})-x64.zip}"
OPUS_FALLBACK_URL="${OPUS_FALLBACK_URL:-https://www.rarewares.org/files/opus/${OPUS_FALLBACK_ZIP}}"
FLAC_URL="${FLAC_URL:-https://www.rarewares.org/files/lossless/flac-${FLAC_VER}-x64.zip}"
VORBIS_URL="${VORBIS_URL:-http://www.rarewares.org/files/ogg/vorbiscomment-${VORBIS_VER}.zip}"
if [ -n "$MUTAGEN_VER" ]; then MUTAGEN_SPEC="mutagen==$MUTAGEN_VER"; else MUTAGEN_SPEC="mutagen"; fi

# fix: these must exist BEFORE any curl -o cache/... or tar -xzf
# (the old split scripts sourced lib-upstream.sh before mkdir, so the
# first run died with `curl: (23) ... cache/nightcore-src-*.tar.gz:
# No such file or directory`).
mkdir -p "$STAGE" "$CACHE" build dist

# Guard for the auto-removes below: every target must live inside the
# script's own build/ tree, and we must still be standing in the
# script's directory. Missing paths are fine (fresh start); anything
# else aborts instead of deleting.
wipe_build() { # wipe_build RELPATH...
    [ "$PWD" = "$ROOT" ] || {
        echo "refusing to delete: expected to be in $ROOT (in $PWD)" >&2
        exit 1
    }
    local d
    for d in "$@"; do
        [ -e "$d" ] || continue
        case "$d" in
            build/*) rm -rf "$ROOT/$d";;
            *) echo "refusing to delete outside build/: $d" >&2; exit 1;;
        esac
    done
}

FETCH_ONLY=0; NO_DOWNLOAD=0; PORTABLE_ONLY=0
WITH_QAAC=0; FULL=0; BUILD_ALL=0; NO_UPX=0
for a in "$@"; do case "$a" in
    --fetch-only) FETCH_ONLY=1;;
    --no-download) NO_DOWNLOAD=1;;
    --portable-only) PORTABLE_ONLY=1;;
    --no-upx) NO_UPX=1;;
    --with-qaac) WITH_QAAC=1;;
    --full) FULL=1;;
    --build-all) BUILD_ALL=1;;
    *) echo "unknown flag: $a" >&2; exit 2;;
esac; done
if [ "$WITH_QAAC" -eq 1 ] && [ "$FULL" -eq 1 ]; then
    echo "pick one of --with-qaac or --full (or --build-all)" >&2; exit 2
fi

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
            # Stale extracts go (tarballs stay cached, so this is cheap);
            # a NIGHTCORE_SRC path above is never touched.
            wipe_build build/upstream-*
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

fetch_upx() {
    if [ "$NO_UPX" -eq 1 ]; then
        echo "upx: off (--no-upx, dist exes may exceed 100MB)"
        return 0
    fi
    if command -v upx >/dev/null 2>&1; then
        echo "upx: system ($(upx --version | head -n 1))"
        return 0
    fi
    mkdir -p "$CACHE/upx"
    if [ ! -x "$CACHE/upx/upx" ]; then
        echo "== fetch upx =="
        dl "https://github.com/upx/upx/releases/download/v${UPX_VER}/upx-${UPX_VER}-amd64_linux.tar.xz" \
            "$CACHE/upx.tar.xz" || { echo "!! upx fetch failed (exes stay unpacked, may exceed 100MB)"; return 0; }
        tmpd="$(mktemp -d)"
        tar -xJf "$CACHE/upx.tar.xz" -C "$tmpd"
        hit="$(find "$tmpd" -name upx -type f | head -n 1)"
        [ -n "$hit" ] || { rm -rf "$tmpd"; echo "!! upx binary not in tarball" >&2; return 0; }
        cp -f "$hit" "$CACHE/upx/upx"; chmod +x "$CACHE/upx/upx"
        rm -rf "$tmpd"
    else
        echo "cached: $CACHE/upx/upx"
    fi
    export PATH="$ROOT/$CACHE/upx:$PATH"
    echo "upx: $("$ROOT/$CACHE/upx/upx" --version | head -n 1)"
}

pack_exes() { # pack_exes FILE... (ffmpeg/ffprobe; no-op under --no-upx)
    [ "$NO_UPX" -eq 0 ] || return 0
    command -v upx >/dev/null 2>&1 || { echo "!! upx missing, exes stay unpacked (may exceed 100MB)" >&2; return 0; }
    local f
    for f in "$@"; do
        [ -f "$f" ] || continue
        upx --best "$f" || echo "!! upx skipped $f" >&2
    done
}

check_size() { # check_size FILE (100MB Cloudflare/Gitea cap)
    local size; size="$(stat -c%s "$1")"
    echo "size: $1 ($size bytes)"
    if [ "$size" -gt 100000000 ]; then
        echo "!! $1 exceeds 100MB: too big for Cloudflare/Gitea" >&2
    fi
}

fetch_tools() {
    echo "== fetch =="
    # versioned names: an unversioned python-embed.zip goes stale on bump
    # (dl skips present files, so the portable tree kept the old python).
    dl "$PY_EMBED_URL" \
        "$CACHE/python-embed-$PY_VER.zip"
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
    wipe_build "$PORTABLE"
    mkdir -p "$PORTABLE/python" "$PORTABLE/app" "$PORTABLE/tools"

    # 1. windows python embed
    if [ -s "$CACHE/python-embed-$PY_VER.zip" ]; then
        unzip -q -o "$CACHE/python-embed-$PY_VER.zip" -d "$PORTABLE/python"
        # allow site + our app dir: _pth name varies (python314._pth)
        PTH="$(ls "$PORTABLE/python"/python*._pth | head -n 1)"
        { echo "../app"; echo "./Lib/site-packages"; grep -v "^#" "$PTH" | grep -v "^\.\./app" || true; echo "import site"; } | awk '!seen[$0]++' > "$PTH.new"; mv "$PTH.new" "$PTH"
        mkdir -p "$PORTABLE/python/Lib/site-packages"
        WHEEL="$(ls -t "$CACHE"/mutagen-*.whl 2>/dev/null | head -n 1 || true)"
        if [ -n "$WHEEL" ]; then unzip -q -o "$WHEEL" -d "$PORTABLE/python/Lib/site-packages"; echo "mutagen from $(basename "$WHEEL")"
        else echo "!! no mutagen wheel in $CACHE (m4a/mp3 postprocess will warn)" >&2; fi
    else
        echo "!! $CACHE/python-embed-$PY_VER.zip missing (need network once)" >&2; exit 1
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
    # Shell completions ride along (PowerShell is the live one on Windows;
    # the rest are tiny and stay in sync for msys2/git-bash/WSL users).
    # The NSIS setup copies the whole payload, so this ships there too.
    mkdir -p "$PORTABLE/completions"
    cp -f "$SRC/completions/"* "$PORTABLE/completions/" 2>/dev/null || true

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
    # per-variant payload dir (--build-all never collides with itself);
    # a stale one from a previous run goes first.
    if [ -z "$tag" ]; then payload="build/payload-setup";
    else payload="build/payload-setup$tag"; fi
    wipe_build "$payload"
    echo "== setup payload ($mode) -> $payload =="

    NSIS="cache/nsis"
    if [ ! -x "$NSIS/a/usr/bin/makensis" ]; then
        echo "== fetch makensis =="
        mkdir -p "$NSIS/a" "$NSIS/b"
        curl -fL --retry 3 -o "$NSIS/nsis.deb" \
            "https://ftp.debian.org/debian/pool/main/n/nsis/nsis_${NSIS_VER}_${NSIS_HOST_ARCH}.deb"
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
    # UPX the two giants so the setup stays under 100MB (qaac untouched).
    pack_exes "$payload/tools/ffmpeg.exe" "$payload/tools/ffprobe.exe"
    du -sh "$payload"
    echo "qaac in setup: $(ls "$payload/tools/qaac.exe" 2>/dev/null || echo NO)"

    mkdir -p build dist
    sed -e "s|@VER@|$VER|g" -e "s|@ROOT@|$ROOT|g" -e "s|@TAG@|$tag|g" \
        -e "s|@PAYLOAD@|$(basename "$payload")|g" \
        -e "s|@OUT@|nightcore-$VER-x86_64-setup$tag.exe|g" \
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
    file "dist/nightcore-$VER-x86_64-setup$tag.exe"
    check_size "dist/nightcore-$VER-x86_64-setup$tag.exe"
}

# ---- main ----
resolve_upstream
echo "versions: arch=x86_64 nightcore=${NIGHTCORE_VERSION:-latest} py=$PY_VER"
echo "versions: sox=$SOX_VER rubberband=$RUBBERBAND_VER lame=$LAME_VER opus=$OPUS_VER libopus=$LIBOPUS_VER"
echo "versions: flac=$FLAC_VER vorbis=$VORBIS_VER mutagen=${MUTAGEN_VER:-latest} nsis=$NSIS_VER upx=$([ "$NO_UPX" -eq 1 ] && echo off || echo "$UPX_VER")"
if [ "$NO_DOWNLOAD" -eq 0 ]; then
    fetch_tools
    fetch_upx
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

if [ "$PORTABLE_ONLY" -eq 0 ]; then
    [ -d build/portable/tools ] || { echo "portable tools missing" >&2; exit 1; }
    for m in $MODES; do build_setup_one "$m"; done
fi
echo "done. outputs in $ROOT/dist + $ROOT/build/portable"
