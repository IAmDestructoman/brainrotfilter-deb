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
    for bin in lb xorriso wget gpg mkfs.vfat mcopy; do
        if ! command -v "$bin" >/dev/null 2>&1; then
            echo "Missing dependency: $bin" >&2
            echo "Install with: sudo apt install live-build xorriso wget gnupg isolinux syslinux-common grub-efi-amd64-bin grub-efi-amd64-signed shim-signed mtools dosfstools" >&2
            exit 1
        fi
    done
    for f in /usr/lib/ISOLINUX/isolinux.bin /usr/lib/ISOLINUX/isohdpfx.bin; do
        if [ ! -f "$f" ]; then
            echo "Missing file: $f (install isolinux + syslinux-common)" >&2
            exit 1
        fi
    done
}

prepare_gpg() {
    # live-build's chroot_archives phase generates a throwaway key to sign
    # the on-the-fly local packages.chroot/ apt repo. In non-interactive
    # (detached / pipe) contexts gpg-agent fails with ENOTTY because it tries
    # to open /dev/tty for pinentry. Pre-seed /root/.gnupg so a passwordless
    # key exists and loopback pinentry is allowed.
    export GNUPGHOME=/root/.gnupg
    install -d -m 700 /root/.gnupg
    cat > /root/.gnupg/gpg.conf <<'EOF'
pinentry-mode loopback
batch
EOF
    cat > /root/.gnupg/gpg-agent.conf <<'EOF'
allow-loopback-pinentry
EOF
    gpgconf --kill gpg-agent 2>/dev/null || true

    if ! gpg --list-secret-keys 2>/dev/null | grep -q livebuild@invalid; then
        echo "[build_iso] Generating throwaway signing key..."
        cat > /tmp/genkey.batch <<'EOF'
%no-protection
Key-Type: RSA
Key-Length: 2048
Name-Real: LiveBuild
Name-Email: livebuild@invalid
Expire-Date: 0
%commit
EOF
        gpg --batch --pinentry-mode loopback --passphrase '' \
            --generate-key /tmp/genkey.batch 2>&1 | tail -20
        rm -f /tmp/genkey.batch
    fi
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

    # Use a CDN-backed mirror (mirrors.edge.kernel.org is behind Fastly)
    # for the *build-time* pulls. archive.ubuntu.com rate-limits (HTTP 429)
    # on large packages like linux-firmware. The final binary still points
    # to archive.ubuntu.com so end-users get the canonical mirror.
    BUILD_MIRROR="http://mirrors.edge.kernel.org/ubuntu/"
    FINAL_MIRROR="http://archive.ubuntu.com/ubuntu/"

    lb config \
        --distribution "$DIST" \
        --architectures "$ARCH" \
        --binary-images iso-hybrid \
        --archive-areas "main universe" \
        --apt-indices false \
        --apt-recommends false \
        --apt-secure false \
        --mirror-bootstrap "$BUILD_MIRROR" \
        --mirror-chroot "$BUILD_MIRROR" \
        --mirror-chroot-security "$BUILD_MIRROR" \
        --mirror-binary "$FINAL_MIRROR" \
        --mirror-binary-security http://security.ubuntu.com/ubuntu/ \
        --debian-installer false \
        --bootloaders grub-pc \
        --bootappend-live "boot=live components quiet splash" \
        --iso-application "BrainrotFilter" \
        --iso-publisher "BrainrotFilter Project" \
        --iso-volume "BRAINROT_${VERSION}"

    # debootstrap's minimal chroot omits gnupg; live-build's archives phase
    # needs `gpg` (not just `gpgv`) when signing the local packages.chroot
    # repo. Inject it into the bootstrap phase via the generated bootstrap
    # config.
    if [ -f config/bootstrap ]; then
        sed -i 's|^LB_BOOTSTRAP_INCLUDE=.*|LB_BOOTSTRAP_INCLUDE="gnupg ca-certificates"|' config/bootstrap
        grep -q '^LB_BOOTSTRAP_INCLUDE=' config/bootstrap || \
            echo 'LB_BOOTSTRAP_INCLUDE="gnupg ca-certificates"' >> config/bootstrap
    fi

    # Overlay our config/ tree onto live-build's config/
    cp -rv "$CONFIG_DIR"/* config/

    # Stage the .deb at a known path inside the chroot so a hook can install
    # it via `dpkg -i` after base packages are in. Using packages.chroot/
    # triggers live-build's local apt repo which requires gpg signing — we
    # sidestep all of that.
    mkdir -p config/includes.chroot_after_packages/opt/brainrot-install
    cp -v "$PACKAGES_DIR"/brainrotfilter_*.deb \
        config/includes.chroot_after_packages/opt/brainrot-install/

    cd ..
}

build_iso() {
    cd "$WORK_DIR"
    echo "Starting lb build (may take 15-30 minutes)..."
    lb build 2>&1 | tee build.log
    cd ..
}

remaster_iso() {
    # live-build's bootloader step on noble leaves the ISO with no usable
    # El Torito record (syslinux can't pull gfxboot-theme-ubuntu, grub-pc
    # path falls through to a missing isohybrid call). Repack with both
    # BIOS (isolinux) and UEFI (shim+grub FAT image) boot records.
    local src
    for candidate in binary.hybrid.iso chroot/binary.hybrid.iso live-image-amd64.hybrid.iso; do
        if [ -f "$WORK_DIR/$candidate" ]; then
            src="$WORK_DIR/$candidate"
            break
        fi
    done
    if [ -z "$src" ]; then
        echo "ERROR: No source ISO found in $WORK_DIR/" >&2
        exit 1
    fi

    echo "[build_iso] Remastering $src with syslinux (BIOS) + shim/grub (UEFI)..."
    local stage=/tmp/brainrot-iso-stage
    local mnt=/tmp/brainrot-iso-mount
    rm -rf "$stage"
    mkdir -p "$stage" "$mnt"
    mount -o loop,ro "$src" "$mnt"
    cp -a "$mnt/." "$stage/"
    umount "$mnt"

    local vmlinuz initrd
    vmlinuz=$(cd "$stage/casper" 2>/dev/null && ls vmlinuz-* 2>/dev/null | head -1)
    initrd=$(cd "$stage/casper" 2>/dev/null && ls initrd.img-* 2>/dev/null | head -1)
    if [ -z "$vmlinuz" ] || [ -z "$initrd" ]; then
        echo "ERROR: No casper kernel/initrd in staged ISO contents" >&2
        exit 1
    fi

    # --- syslinux (BIOS)
    mkdir -p "$stage/isolinux"
    cp /usr/lib/ISOLINUX/isolinux.bin "$stage/isolinux/"
    cp /usr/lib/syslinux/modules/bios/*.c32 "$stage/isolinux/" 2>/dev/null || true
    cat > "$stage/isolinux/isolinux.cfg" <<EOF
UI vesamenu.c32
PROMPT 0
TIMEOUT 50
DEFAULT live

MENU TITLE BrainrotFilter Appliance $VERSION

LABEL live
    MENU LABEL BrainrotFilter Appliance (Live)
    MENU DEFAULT
    KERNEL /casper/$vmlinuz
    APPEND initrd=/casper/$initrd boot=casper quiet splash ---

LABEL safe
    MENU LABEL BrainrotFilter Appliance (Safe mode)
    KERNEL /casper/$vmlinuz
    APPEND initrd=/casper/$initrd boot=casper nomodeset ---

LABEL mem
    MENU LABEL Memory test
    KERNEL /casper/memtest
EOF

    # --- grub config (used both by the EFI FAT image and as a fallback)
    mkdir -p "$stage/boot/grub" "$stage/EFI/BOOT"
    cat > "$stage/boot/grub/grub.cfg" <<EOF
set default=0
set timeout=5

menuentry "BrainrotFilter Appliance (Live)" {
    linux /casper/$vmlinuz boot=casper quiet splash ---
    initrd /casper/$initrd
}

menuentry "BrainrotFilter Appliance (Safe mode)" {
    linux /casper/$vmlinuz boot=casper nomodeset ---
    initrd /casper/$initrd
}

menuentry "Memory test" {
    linux16 /casper/memtest
}
EOF

    # --- UEFI FAT image (El Torito alt boot)
    local shim_src=""
    [ -f /usr/lib/shim/shimx64.efi.signed ] && shim_src=/usr/lib/shim/shimx64.efi.signed
    local grub_src=""
    for c in /usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed \
             /usr/lib/grub/x86_64-efi/monolithic/grubx64.efi; do
        [ -f "$c" ] && grub_src="$c" && break
    done
    [ -z "$grub_src" ] && { echo "ERROR: grubx64 not found" >&2; exit 1; }

    if [ -n "$shim_src" ]; then
        cp "$shim_src" "$stage/EFI/BOOT/BOOTX64.EFI"
        cp "$grub_src" "$stage/EFI/BOOT/grubx64.efi"
    else
        cp "$grub_src" "$stage/EFI/BOOT/BOOTX64.EFI"
    fi
    cat > "$stage/EFI/BOOT/grub.cfg" <<'EOF'
search --set=root --file /boot/grub/grub.cfg
set prefix=($root)/boot/grub
configfile /boot/grub/grub.cfg
EOF

    local efi_img="$stage/boot/grub/efi.img"
    dd if=/dev/zero of="$efi_img" bs=1M count=10 status=none
    mkfs.vfat -F 16 -n EFIBOOT "$efi_img" >/dev/null
    mmd -i "$efi_img" ::/EFI ::/EFI/BOOT
    mcopy -i "$efi_img" "$stage/EFI/BOOT/BOOTX64.EFI" ::/EFI/BOOT/BOOTX64.EFI
    [ -f "$stage/EFI/BOOT/grubx64.efi" ] && \
        mcopy -i "$efi_img" "$stage/EFI/BOOT/grubx64.efi" ::/EFI/BOOT/grubx64.efi
    mcopy -i "$efi_img" "$stage/EFI/BOOT/grub.cfg" ::/EFI/BOOT/grub.cfg

    # --- Pack
    rm -f "$WORK_DIR/$ISO_NAME.remaster"
    xorriso -as mkisofs \
        -iso-level 3 \
        -full-iso9660-filenames \
        -volid "BRAINROT_${VERSION}" \
        -eltorito-boot isolinux/isolinux.bin \
        -eltorito-catalog isolinux/boot.cat \
        -no-emul-boot -boot-load-size 4 -boot-info-table \
        -eltorito-alt-boot \
        -e boot/grub/efi.img \
        -no-emul-boot \
        -isohybrid-mbr /usr/lib/ISOLINUX/isohdpfx.bin \
        -isohybrid-gpt-basdat \
        -output "$WORK_DIR/$ISO_NAME.remaster" \
        "$stage"
    rm -rf "$stage"

    if [ ! -s "$WORK_DIR/$ISO_NAME.remaster" ]; then
        echo "ERROR: xorriso did not produce an ISO" >&2
        exit 1
    fi
    mv "$WORK_DIR/$ISO_NAME.remaster" "$WORK_DIR/$ISO_NAME"
}

publish_iso() {
    remaster_iso

    local iso_src="$WORK_DIR/$ISO_NAME"
    cp -v "$iso_src" "./$ISO_NAME"
    echo
    echo "======================================"
    echo "  ISO built: $(pwd)/$ISO_NAME"
    echo "  Size:     $(du -h "./$ISO_NAME" | cut -f1)"
    echo "  SHA256:   $(sha256sum "./$ISO_NAME" | cut -d' ' -f1)"
    echo "  Boot:     BIOS + UEFI hybrid (Rufus / balenaEtcher compatible)"
    echo "======================================"
}

main() {
    need_root
    check_deps
    prepare_gpg
    check_deb
    clean_work
    configure
    build_iso
    publish_iso
}

main "$@"
