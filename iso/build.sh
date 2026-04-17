#!/bin/bash
# BrainrotFilter appliance ISO builder.
# Runs on an Ubuntu 22.04+ host with live-build installed.

set -euo pipefail

DIST="noble"          # Ubuntu 24.04 LTS
ARCH="amd64"
VERSION="1.1.0"
ISO_NAME="brainrotfilter-${VERSION}.iso"
WORK_DIR="$(pwd)/work"
CONFIG_DIR="$(pwd)/config"
PACKAGES_DIR="$(pwd)/packages"

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "Must run as root (or with sudo)." >&2
        exit 1
    fi
}

check_deps() {
    for bin in lb xorriso wget gpg; do
        if ! command -v "$bin" >/dev/null 2>&1; then
            echo "Missing dependency: $bin" >&2
            echo "Install with: sudo apt install live-build xorriso wget gnupg" >&2
            exit 1
        fi
    done
}

check_deb() {
    local count
    count=$(ls -1 "$PACKAGES_DIR"/brainrotfilter_*.deb 2>/dev/null | wc -l)
    if [ "$count" -eq 0 ]; then
        echo "ERROR: No brainrotfilter_*.deb found in $PACKAGES_DIR/" >&2
        echo "Run 'dpkg-buildpackage -us -uc -b' from the repo root first," >&2
        echo "then copy the resulting .deb into $PACKAGES_DIR/" >&2
        exit 1
    fi
    echo "Found .deb(s):"
    ls -1 "$PACKAGES_DIR"/brainrotfilter_*.deb
}

clean_work() {
    if [ -d "$WORK_DIR" ]; then
        echo "Cleaning previous work dir..."
        cd "$WORK_DIR"
        lb clean --purge >/dev/null 2>&1 || true
        cd ..
        rm -rf "$WORK_DIR"
    fi
    mkdir -p "$WORK_DIR"
}

configure() {
    cd "$WORK_DIR"

    lb config \
        --distribution "$DIST" \
        --architectures "$ARCH" \
        --binary-images iso-hybrid \
        --archive-areas "main universe" \
        --apt-indices false \
        --apt-recommends false \
        --mirror-bootstrap http://archive.ubuntu.com/ubuntu/ \
        --mirror-binary http://archive.ubuntu.com/ubuntu/ \
        --debian-installer none \
        --bootappend-live "boot=live components quiet splash" \
        --iso-application "BrainrotFilter" \
        --iso-publisher "BrainrotFilter Project" \
        --iso-volume "BRAINROT_${VERSION}"

    # Overlay our config/ tree onto live-build's config/
    cp -rv "$CONFIG_DIR"/* config/

    # Inject .deb into packages.chroot/ so it's installed during build
    mkdir -p config/packages.chroot
    cp -v "$PACKAGES_DIR"/brainrotfilter_*.deb config/packages.chroot/

    cd ..
}

build_iso() {
    cd "$WORK_DIR"
    echo "Starting lb build (may take 15-30 minutes)..."
    lb build 2>&1 | tee build.log
    cd ..
}

publish_iso() {
    local iso_src
    iso_src=$(ls -1 "$WORK_DIR"/live-image-amd64.hybrid.iso 2>/dev/null | head -1)
    if [ -z "$iso_src" ]; then
        echo "ERROR: No ISO produced. See $WORK_DIR/build.log" >&2
        exit 1
    fi
    cp -v "$iso_src" "./$ISO_NAME"
    echo
    echo "======================================"
    echo "  ISO built: $(pwd)/$ISO_NAME"
    echo "  Size:     $(du -h "./$ISO_NAME" | cut -f1)"
    echo "  SHA256:   $(sha256sum "./$ISO_NAME" | cut -d' ' -f1)"
    echo "======================================"
}

main() {
    need_root
    check_deps
    check_deb
    clean_work
    configure
    build_iso
    publish_iso
}

main "$@"
