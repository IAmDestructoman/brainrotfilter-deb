#!/usr/bin/env bash
# Build the BrainrotFilter .deb using WSL Ubuntu.
#
# Run from Windows (PowerShell, cmd, or Git Bash):
#     bash scripts/build_deb.sh
# Or from WSL directly:
#     ./scripts/build_deb.sh
#
# Output lands in dist/brainrotfilter_<version>_all.deb
set -euo pipefail

REPO_WSL="/mnt/e/Code/brainrotfilter-deb"
BUILD_DIR_NAME="brainrotfilter-build"

# Git Bash / MSYS / cmd: bounce into WSL. Set MSYS_NO_PATHCONV=1 so
# Git Bash doesn't translate /mnt/... into C:/Program Files/Git/mnt/...
if ! grep -qi microsoft /proc/version 2>/dev/null || grep -qi mingw /proc/version 2>/dev/null; then
    export MSYS_NO_PATHCONV=1
    exec wsl -- bash "$REPO_WSL/scripts/build_deb.sh" "$@"
fi

BUILD_DIR="$HOME/$BUILD_DIR_NAME"

cd "$REPO_WSL"

echo "[build_deb] Syncing source tree to $BUILD_DIR..."
rsync -a --delete \
    --exclude='.git' \
    --exclude='debian/.debhelper' \
    --exclude='debian/brainrotfilter' \
    --exclude='debian/files' \
    --exclude='debian/*.substvars' \
    --exclude='debian/debhelper-build-stamp' \
    --exclude='dist' \
    --exclude='__pycache__' \
    "$REPO_WSL/" "$BUILD_DIR/"

echo "[build_deb] Running dpkg-buildpackage..."
cd "$BUILD_DIR"
dpkg-buildpackage -us -uc -b

VERSION=$(dpkg-parsechangelog -l debian/changelog -S Version)
DEB_NAME="brainrotfilter_${VERSION}_all.deb"
PARENT_DIR=$(dirname "$BUILD_DIR")

mkdir -p "$REPO_WSL/dist"
cp "$PARENT_DIR/$DEB_NAME" "$REPO_WSL/dist/$DEB_NAME"

echo "[build_deb] Done: dist/$DEB_NAME"
ls -la "$REPO_WSL/dist/$DEB_NAME"
