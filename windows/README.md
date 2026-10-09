# nightcore.exe - Windows builds

There are two ways to install this: single-file bundle with everything inside, or
setup. The setup is more flexible because you can drag your own Apple DLLs for AAC
encoding with qaac.

You can also build with the Apple DLLs but that's legally grey.

## Build

One script does everything on Linux:

```bash
./build.sh                                      # portable + setup + bundle
./build.sh --fetch-only                         # only fill tools/ + cache/
./build.sh --no-download                        # stage from cache/tools only
./build.sh --portable-only                      # stop after build/portable
./build.sh --skip-setup | --skip-bundle         # only one installer type
./build.sh [--with-qaac|--full|--build-all]     # qaac variants
```

| Flag          | Setup / Bundle result                                              |
|---------------|--------------------------------------------------------------------|
| _(default)_   | shareable-safe: **no qaac / Apple DLLs**, even if staged           |
| `--with-qaac` | + `qaac.exe`, **no Apple DLLs** (`-qaac` names, drag-drop yours)   |
| `--full`      | + qaac + your Apple DLLs (`-full` names, personal only)            |
| `--build-all` | all three                                                          |

Without a flag, `qaac.exe`, `refalac.exe` and the 8 Apple DLLs are stripped from
every payload. `--with-qaac` keeps the exe but strips the DLLs (thin setup: drag-drop
your own into `tools\`); `--full` keeps both (personal builds only).

## Pin versions

Every tool version plus nightcore itself is pinned in `versions.env`:

| Variable | Pins | Default |
|---|---|---|
| `NIGHTCORE_VERSION` | nightcore app source tag (empty = latest) | _(latest)_ |
| `PY_VER` | Windows Python embed | `3.12.7` |
| `WINE_PY_VER` | Windows Python under Wine (defaults to `PY_VER`) | _(follows `PY_VER`)_ |
| `SOX_VER` / `RUBBERBAND_VER` / `LAME_VER` | sox / rubberband / lame | `14.4.2` / `3.3.0` / `3.100.1` |
| `OPUS_VER` / `LIBOPUS_VER` / `OPUS_FALLBACK_ZIP` | opus-tools (+ fallback zip) | `0.2-49-g4b96717` / `1.6.1-11-g788cc89` |
| `FLAC_VER` / `VORBIS_VER` | flac / vorbiscomment | `1.4.3` / `1.2.0` |
| `MUTAGEN_VER` / `PYINSTALLER_VER` | PyPI packages (empty = latest) | _(latest)_ |
| `NSIS_VER` | NSIS .debs for the setup | `3.12-1` |
| `FFMPEG_URL` / `SOX_URL` / ... | full download URLs (mirrors) | _(built from the versions above)_ |

```bash
./build.sh --fetch-only   # after bumping, re-fetch just tools/ + cache/
NIGHTCORE_VERSION=v2026.10.09-1 ./build.sh --full   # one-off pin, no file edit
SOX_VER=14.4.3 ./build.sh --fetch-only              # one-off bump, no file edit
```

The effective pins print at the start of every run (`versions: ...`).
Windows target is fixed x86_64: outputs are named
`nightcore-<ver>-x86_64-setup...` / `nightcore-<ver>-x86_64-bundle...`.
`NIGHTCORE_SRC` (a local path) still overrides `NIGHTCORE_VERSION` when set.
