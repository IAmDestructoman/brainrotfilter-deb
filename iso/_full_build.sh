#!/bin/bash
# End-to-end ISO build: golden image + installer chroot + squashfs +
# remaster. All intermediate artifacts land under ~/brainrot-build/
# (persistent across WSL restarts, unlike /tmp which is tmpfs).
#
# Invoke with: sudo bash _full_build.sh
set -euo pipefail

BUILD=${BUILD:-/home/ytfilter/brainrot-build}
CHROOT=/home/ytfilter/brainrot-iso/work/chroot
REPO=/mnt/e/Code/brainrotfilter-deb

mkdir -p "$BUILD" "$BUILD/stage"
cd "$BUILD"

echo "=== 1/4 building golden image ==="
bash "$REPO/iso/build_golden_image.sh" "$CHROOT" "$BUILD/brainrot-golden.img.zst"

echo "=== 2/4 building installer chroot ==="
rm -rf "$BUILD/installer-chroot"
bash "$REPO/iso/build_installer_chroot.sh" "$BUILD/installer-chroot"

echo "=== 3/4 staging ISO tree ==="
STAGE="$BUILD/stage"
# Start from a fresh stage; extract boot scaffolding from the last
# published ISO if we don't have it cached.
mkdir -p "$STAGE/casper" "$STAGE/isolinux" "$STAGE/boot/grub"
if [ ! -f "$STAGE/isolinux/isolinux.bin" ]; then
    echo "  seeding isolinux + efi.img from last published ISO..."
    TMPMNT=$(mktemp -d)
    mount -o loop,ro /mnt/e/Code/brainrotfilter-deb/dist/brainrotfilter-1.1.0.iso "$TMPMNT"
    rsync -a --exclude='casper/filesystem.squashfs' \
              --exclude='casper/vmlinuz*' \
              --exclude='casper/initrd.img*' \
              --exclude='brainrot-golden.img.zst' \
              "$TMPMNT/." "$STAGE/"
    umount "$TMPMNT"
    rmdir "$TMPMNT"
fi

cp "$BUILD/brainrot-golden.img.zst" "$STAGE/brainrot-golden.img.zst"

mksquashfs "$BUILD/installer-chroot" "$BUILD/filesystem.squashfs" \
    -noappend -comp xz -no-progress 2>&1 | tail -2
cp "$BUILD/filesystem.squashfs" "$STAGE/casper/filesystem.squashfs"
cp "$BUILD/installer-chroot"/boot/vmlinuz-* "$STAGE/casper/"
cp "$BUILD/installer-chroot"/boot/initrd.img-* "$STAGE/casper/"

echo "=== 4/4 xorriso remaster ==="
rm -f "$BUILD/brainrotfilter-1.1.0.iso"
xorriso -as mkisofs \
    -iso-level 3 \
    -full-iso9660-filenames \
    -volid BRAINROT_1.1.0 \
    -eltorito-boot isolinux/isolinux.bin \
    -eltorito-catalog isolinux/boot.cat \
    -no-emul-boot -boot-load-size 4 -boot-info-table \
    -eltorito-alt-boot \
    -e boot/grub/efi.img \
    -no-emul-boot \
    -isohybrid-mbr /usr/lib/ISOLINUX/isohdpfx.bin \
    -isohybrid-gpt-basdat \
    -output "$BUILD/brainrotfilter-1.1.0.iso" \
    "$STAGE"

cp "$BUILD/brainrotfilter-1.1.0.iso" /mnt/e/Code/brainrotfilter-deb/dist/brainrotfilter-1.1.0.iso
chmod 644 /mnt/e/Code/brainrotfilter-deb/dist/brainrotfilter-1.1.0.iso

echo
echo "=== DONE ==="
sha256sum /mnt/e/Code/brainrotfilter-deb/dist/brainrotfilter-1.1.0.iso
du -h /mnt/e/Code/brainrotfilter-deb/dist/brainrotfilter-1.1.0.iso
