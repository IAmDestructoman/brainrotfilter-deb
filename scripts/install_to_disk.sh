#!/bin/bash
# install_to_disk.sh - copy the running live appliance onto an internal disk.
# Invoked from the TUI "Install to Disk" action. Runs as root via sudo.
#
# What it does:
#   1. Enumerates candidate disks (lsblk) and skips the live-boot media.
#   2. Prompts for a target, double-confirms, then wipes it.
#   3. Creates GPT with 512 MiB EFI (FAT32) + rest as ext4.
#   4. Extracts the running casper/filesystem.squashfs into the new root.
#   5. Preserves the live session's /etc/brainrotfilter, /var/lib/brainrotfilter,
#      /etc/systemd/network, /etc/netplan so wizard progress survives.
#   6. Writes /etc/fstab by UUID.
#   7. Installs GRUB for UEFI (x86_64-efi + shim) and BIOS (i386-pc) so the
#      target boots on any firmware.
#   8. Prompts the operator to reboot.
set -euo pipefail

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
BOLD=$'\033[1m'; NC=$'\033[0m'

die() { echo "${RED}ERROR:${NC} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root"

# -- Locate the live squashfs source --
SQUASHFS=""
for c in /cdrom/casper/filesystem.squashfs \
         /run/live/medium/casper/filesystem.squashfs \
         /live/image/casper/filesystem.squashfs; do
    if [ -r "$c" ]; then SQUASHFS="$c"; break; fi
done
[ -n "$SQUASHFS" ] || die "can't find casper/filesystem.squashfs; is this a live boot?"

# -- Determine the boot media's parent device (so we refuse to wipe it) --
BOOT_DEV=""
for mnt in /cdrom /run/live/medium /live/image; do
    if mountpoint -q "$mnt" 2>/dev/null; then
        src=$(findmnt -no SOURCE "$mnt" | head -1)
        if [ -n "$src" ]; then
            pk=$(lsblk -no PKNAME "$src" 2>/dev/null | head -1)
            [ -n "$pk" ] && BOOT_DEV="/dev/$pk"
        fi
        break
    fi
done

echo
echo "${BOLD}=== BrainrotFilter: Install to Disk ===${NC}"
echo
echo "Source: $SQUASHFS"
[ -n "$BOOT_DEV" ] && echo "Boot media (will be skipped): $BOOT_DEV"
echo

# -- List candidate disks (skip the boot media) --
mapfile -t DISK_LINES < <(lsblk -dpno NAME,SIZE,MODEL,TYPE 2>/dev/null | awk '$NF=="disk"')
CANDIDATES=()
echo "${BOLD}Available disks:${NC}"
i=0
for line in "${DISK_LINES[@]}"; do
    dev=$(awk '{print $1}' <<<"$line")
    if [ "$dev" = "$BOOT_DEV" ]; then
        printf '     %s  [BOOT MEDIA - skipped]\n' "$line"
        continue
    fi
    i=$((i+1))
    CANDIDATES+=("$dev")
    printf '  %2d) %s\n' "$i" "$line"
done
echo

if [ ${#CANDIDATES[@]} -eq 0 ]; then
    die "no installable disks detected"
fi

# -- Pick target --
read -rp "Pick target disk number (or 'q' to cancel): " PICK
[ "$PICK" = "q" ] || [ -z "$PICK" ] && { echo "Cancelled."; exit 0; }
[[ "$PICK" =~ ^[0-9]+$ ]] || die "not a number"
[ "$PICK" -ge 1 ] && [ "$PICK" -le ${#CANDIDATES[@]} ] || die "out of range"
TARGET="${CANDIDATES[$((PICK-1))]}"
[ "$TARGET" != "$BOOT_DEV" ] || die "refusing to wipe the boot media"

echo
echo "${RED}${BOLD}>>> ALL DATA ON $TARGET WILL BE ERASED <<<${NC}"
lsblk "$TARGET"
echo
read -rp "Type ${BOLD}YES${NC} (all caps) to proceed: " CONFIRM
[ "$CONFIRM" = "YES" ] || { echo "Cancelled."; exit 0; }

# -- Unmount / swapoff anything on the target --
echo "Preparing target..."
for p in $(lsblk -pnlo NAME "$TARGET" | tail -n +2); do
    umount -q "$p" 2>/dev/null || true
    swapoff "$p" 2>/dev/null || true
done
wipefs -a "$TARGET" >/dev/null 2>&1 || true
sgdisk --zap-all "$TARGET" >/dev/null 2>&1 || true

# -- Partition: GPT with
#     1  1 MiB     BIOS Boot Partition (type ef02, no filesystem)
#     2  512 MiB   EFI System Partition (FAT32, type ef00)
#     3  rest      brainrot-root (ext4)
# The BIOS Boot Partition is where GRUB embeds its i386-pc stage on
# GPT disks; without it `grub-install --target=i386-pc` refuses to
# proceed ("GPT partition label contains no BIOS Boot Partition").
# UEFI-only boxes ignore it; BIOS-only boxes need it.
echo "Partitioning..."
parted -s "$TARGET" mklabel gpt
parted -s "$TARGET" mkpart BIOS-boot 1MiB 2MiB
parted -s "$TARGET" set 1 bios_grub on
parted -s "$TARGET" mkpart ESP fat32 2MiB 514MiB
parted -s "$TARGET" set 2 esp on
parted -s "$TARGET" set 2 boot on
parted -s "$TARGET" mkpart brainrot-root ext4 514MiB 100%
partprobe "$TARGET" 2>/dev/null || true
udevadm settle

# nvme / mmcblk use <dev>p<n>, everything else uses <dev><n>
case "$TARGET" in
    *nvme*|*mmcblk*) P_ESP="${TARGET}p2"; P_ROOT="${TARGET}p3" ;;
    *)               P_ESP="${TARGET}2";  P_ROOT="${TARGET}3" ;;
esac

echo "Formatting..."
mkfs.vfat -F32 -n ESP "$P_ESP" >/dev/null
mkfs.ext4 -F -L brainrot-root "$P_ROOT" >/dev/null

# -- Mount --
MNT=/tmp/brainrot-install-root
mkdir -p "$MNT"
mount "$P_ROOT" "$MNT"
mkdir -p "$MNT/boot/efi"
mount "$P_ESP" "$MNT/boot/efi"

# -- Extract rootfs --
echo "Extracting root filesystem (this takes 2-5 minutes)..."
unsquashfs -f -d "$MNT" "$SQUASHFS" >/dev/null 2>&1

# -- Preserve live session state so wizard / bridge / SSH / iptables
#    / root password all carry over to the installed system.
echo "Preserving live session state..."

# Directories: rsync recursively.
for src in /etc/brainrotfilter \
           /var/lib/brainrotfilter \
           /etc/systemd/network \
           /etc/netplan \
           /etc/sudoers.d \
           /etc/iptables \
           /etc/ssh/sshd_config.d; do
    if [ -d "$src" ] && [ -n "$(ls -A "$src" 2>/dev/null)" ]; then
        mkdir -p "$MNT$src"
        rsync -a "$src/" "$MNT$src/"
    fi
done

# Individual files: account databases for the operator's root password
# (set via TUI option 3 or 7) + anything else modified at runtime.
# NB: `[ -f "$f" ] && cp ...` under `set -e` exits the script when the
# test fails (e.g. /etc/subuid missing on a stripped image). Use `if`.
for f in /etc/passwd /etc/shadow /etc/group /etc/gshadow /etc/subuid /etc/subgid; do
    if [ -f "$f" ]; then
        cp -a "$f" "$MNT$f"
    fi
done

# Preserve "unit enable state" by mirroring selected target.wants
# symlink directories. This carries over:
#   - SSH enabled/unmasked state (sockets.target.wants/ssh.socket,
#     multi-user.target.wants/ssh.service)
for wants in /etc/systemd/system/multi-user.target.wants \
             /etc/systemd/system/sockets.target.wants \
             /etc/systemd/system/network-pre.target.wants \
             /etc/systemd/system/default.target.wants; do
    if [ -d "$wants" ]; then
        mkdir -p "$MNT$wants"
        rsync -a "$wants/" "$MNT$wants/"
    fi
done

# Mask symlinks (ssh.service -> /dev/null et al) + non-mask symlinks
# live directly in /etc/systemd/system/. Use `find` so no-match doesn't
# leak the literal glob into the loop body.
find /etc/systemd/system -maxdepth 1 -type l 2>/dev/null | while read -r ln; do
    cp -a "$ln" "$MNT$ln" 2>/dev/null || true
done

# Unit override directories (e.g. getty@tty1.service.d/autologin.conf)
# — shipped in the squashfs by the harden hook, but copy again in case
# the operator customized them at runtime.
find /etc/systemd/system -maxdepth 1 -type d -name '*.d' 2>/dev/null | while read -r d; do
    mkdir -p "$MNT$d"
    cp -a "$d/." "$MNT$d/" 2>/dev/null || true
done

# -- fstab --
ESP_UUID=$(blkid -s UUID -o value "$P_ESP")
ROOT_UUID=$(blkid -s UUID -o value "$P_ROOT")
cat > "$MNT/etc/fstab" <<FSTAB
# Written by BrainrotFilter install_to_disk.sh
UUID=$ROOT_UUID  /          ext4  errors=remount-ro  0 1
UUID=$ESP_UUID   /boot/efi  vfat  umask=0077        0 1
FSTAB

# -- Bind-mount for chroot ops --
for d in dev dev/pts proc sys run; do
    mount --rbind "/$d" "$MNT/$d"
done

# -- Install GRUB for both firmware paths --
echo "Installing GRUB..."
chroot "$MNT" grub-install --target=x86_64-efi \
    --efi-directory=/boot/efi \
    --bootloader-id=BrainrotFilter \
    --recheck --no-floppy 2>&1 | tail -3 || true
chroot "$MNT" grub-install --target=i386-pc \
    --recheck --no-floppy "$TARGET" 2>&1 | tail -3 || true
chroot "$MNT" update-grub 2>&1 | tail -3

# -- Installed system doesn't need the live-only firstboot netplan --
chroot "$MNT" rm -f /etc/netplan/00-brainrot-firstboot.yaml

# -- Wipe any SSH host keys so each installed appliance gets its own.
# The live session may have generated keys when the operator enabled
# SSH for setup/debugging; carrying them to the installed disk would
# reuse the same fingerprint across every install. sshd regenerates
# on first enable (TUI option 7 runs ssh-keygen -A).
rm -f "$MNT"/etc/ssh/ssh_host_*_key "$MNT"/etc/ssh/ssh_host_*_key.pub

# -- Unmount cleanly --
echo "Unmounting..."
for d in run sys proc dev/pts dev; do
    umount -R "$MNT/$d" 2>/dev/null || umount -l "$MNT/$d" 2>/dev/null || true
done
umount "$MNT/boot/efi" 2>/dev/null || true
umount "$MNT" 2>/dev/null || true

echo
echo "${GREEN}${BOLD}Install complete.${NC}"
echo "Target: $TARGET"
echo "After reboot, remove the live media. Installed system boots from $TARGET."
