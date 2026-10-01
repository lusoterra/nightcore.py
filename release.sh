#!/usr/bin/env bash
# Build a signed release tarball for the `nightcore` (non-git) package.
#
# Reads the version from pyproject.toml, archives the tracked tree with
# `git archive`, checksums it, and PGP-signs it. You then upload the
# tarball + .sig to a Gitea release yourself; the script prints the exact
# sha256sums lines to paste into ../nightcore-pkgbuild/PKGBUILD.
#
# Usage:
#   ./release.sh
#
# Env overrides:
#   TAG     release tag (default: v<VERSION>)
#   GPGKEY  signing key id (default: 13EA3CFB9ABC093823509BD668C62637ED987692)
#   OUTDIR  where to write artifacts (default: ./dist-release)
set -euo pipefail

cd "$(dirname "$0")"

VERSION="$(python3 -c 'import re;print(re.search(r"(?m)^\[project\][^\[]*?^version\s*=\s*[\"'"'"']([^\"'"'"']+)", open("pyproject.toml").read(), re.S).group(1))')"
TAG="${TAG:-v$VERSION}"
GPGKEY="${GPGKEY:-13EA3CFB9ABC093823509BD668C62637ED987692}"
OUTDIR="${OUTDIR:-dist-release}"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "warning: working tree has uncommitted changes; the tarball is built" >&2
    echo "warning: from HEAD, not from the working tree." >&2
fi

mkdir -p "$OUTDIR"
BASENAME="nightcore-$VERSION"
TARBALL="$OUTDIR/$BASENAME.tar.gz"

git archive --format=tar.gz --prefix="$BASENAME/" -o "$TARBALL" HEAD
sha256sum "$TARBALL" | tee "$TARBALL.sha256"
gpg --local-user "$GPGKEY" --armor --detach-sign -o "$TARBALL.sig" "$TARBALL"

echo "--- verifying ---"
sha256sum -c "$TARBALL.sha256"
gpg --verify "$TARBALL.sig" "$TARBALL"

SUM="$(cut -d' ' -f1 "$TARBALL.sha256")"
echo
echo "Upload these two files to the '$TAG' release on Gitea:"
echo "  $TARBALL"
echo "  $TARBALL.sig"
echo
echo "Tag the release commit if you have not already:"
echo "  git tag -s '$TAG' -m 'nightcore $VERSION'   # uses \$GPGKEY by default"
echo "  git push origin '$TAG'"
echo
echo "Then paste this into ../nightcore-pkgbuild/PKGBUILD (replacing the placeholder):"
echo "  sha256sums=('$SUM'"
echo "              'SKIP')"
echo
echo "Expected release URLs:"
echo "  https://git.lusosterone.net/lev/nightcore/releases/download/$TAG/$BASENAME.tar.gz"
echo "  https://git.lusosterone.net/lev/nightcore/releases/download/$TAG/$BASENAME.tar.gz.sig"
