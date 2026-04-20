#!/bin/bash
# Pack /tmp/iso-stage into /tmp/brainrotfilter-1.1.0.iso as a hybrid
# BIOS + UEFI ISO using xorriso. Reads the squashfs + golden image +
# kernel + initrd from /tmp/iso-stage/.
set -euo pipefail

STAGE=/tmp/iso-stage
OUT=/tmp/brainrotfilter-1.1.0.iso
VOLID=BRAINROT_1.1.0

[ -d "$STAGE" ] || { echo "ERROR: $STAGE missing" >&2; exit 1; }
[ -f "$STAGE/casper/filesystem.squashfs" ] || { echo "ERROR: squashfs missing" >&2; exit 1; }
[ -f "$STAGE/isolinux/isolinux.bin" ] || { echo "ERROR: isolinux.bin missing at $STAGE/isolinux" >&2; exit 1; }

rm -f "$OUT"
xorriso -as mkisofs \
    -iso-level 3 \
    -full-iso9660-filenames \
    -volid "$VOLID" \
    -eltorito-boot isolinux/isolinux.bin \
    -eltorito-catalog isolinux/boot.cat \
    -no-emul-boot -boot-load-size 4 -boot-info-table \
    -eltorito-alt-boot \
    -e boot/grub/efi.img \
    -no-emul-boot \
    -isohybrid-mbr /usr/lib/ISOLINUX/isohdpfx.bin \
    -isohybrid-gpt-basdat \
    -output "$OUT" \
    "$STAGE"

echo
file "$OUT"
sha256sum "$OUT"
du -h "$OUT"
