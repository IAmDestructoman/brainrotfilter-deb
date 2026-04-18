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

# -- Locate the live squashfs source.
#    Normal live boot paths first; then fall through to any attached
#    CD/DVD that holds a valid casper ISO. That means this installer
#    works even when booted from an already-installed (possibly broken)
#    disk — as long as the current BrainrotFilter ISO is in the drive,
#    we can reinstall over the host.
SQUASHFS=""
for c in /cdrom/casper/filesystem.squashfs \
         /run/live/medium/casper/filesystem.squashfs \
         /live/image/casper/filesystem.squashfs; do
    if [ -r "$c" ]; then SQUASHFS="$c"; break; fi
done

SR_MOUNT=""
if [ -z "$SQUASHFS" ]; then
    for d in /dev/sr0 /dev/sr1 /dev/cdrom /dev/dvd; do
        [ -b "$d" ] || continue
        m=$(mktemp -d /tmp/brainrot-iso-src.XXXXXX)
        if mount -o ro "$d" "$m" 2>/dev/null; then
            if [ -r "$m/casper/filesystem.squashfs" ]; then
                SQUASHFS="$m/casper/filesystem.squashfs"
                SR_MOUNT="$m"
                echo "Found ISO source on $d (mounted at $m)"
                break
            fi
            umount "$m" 2>/dev/null || true
        fi
        rmdir "$m" 2>/dev/null || true
    done
fi

[ -n "$SQUASHFS" ] || die "can't find casper/filesystem.squashfs — attach the BrainrotFilter ISO to the DVD drive and try again"

# -- Determine the boot media's parent device (so we refuse to wipe it) --
BOOT_DEV=""
for mnt in /cdrom /run/live/medium /live/image; do
    if mountpoint -q "$mnt" 2>/dev/null; then
        src=$(findmnt -no SOURCE "$mnt" | head -1)
        if [ -n "$src" ]; then
            pk=$(lsblk -no PKNAME "$src" 2>/dev/null | head -1 || true)
            if [ -n "$pk" ]; then
                BOOT_DEV="/dev/$pk"
            fi
        fi
        break
    fi
done

echo
echo "${BOLD}=== BrainrotFilter: Install to Disk ===${NC}"
echo
echo "Source: $SQUASHFS"
if [ -n "$BOOT_DEV" ]; then
    echo "Boot media (will be skipped): $BOOT_DEV"
fi
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
if [ -z "$PICK" ] || [ "$PICK" = "q" ] || [ "$PICK" = "Q" ]; then
    echo "Cancelled."; exit 0
fi
[[ "$PICK" =~ ^[0-9]+$ ]] || die "not a number"
if [ "$PICK" -lt 1 ] || [ "$PICK" -gt "${#CANDIDATES[@]}" ]; then
    die "out of range"
fi
TARGET="${CANDIDATES[$((PICK-1))]}"
if [ -n "$BOOT_DEV" ] && [ "$TARGET" = "$BOOT_DEV" ]; then
    die "refusing to wipe the boot media"
fi

# Also refuse to wipe the currently-running system root (can happen
# when booted from the installed disk rather than the live ISO).
# On a live boot, findmnt returns "overlay" / "/cow" which isn't a
# block device — resolve safely so `set -e` doesn't trip on the
# empty-lsblk test.
RUNNING_DEV=""
RUNNING_ROOT=$(findmnt -no SOURCE / 2>/dev/null | head -1 || true)
if [ -n "$RUNNING_ROOT" ] && [ -b "$RUNNING_ROOT" ]; then
    pk=$(lsblk -no PKNAME "$RUNNING_ROOT" 2>/dev/null | head -1 || true)
    if [ -n "$pk" ]; then
        RUNNING_DEV="/dev/$pk"
    fi
fi
if [ -n "$RUNNING_DEV" ] && [ "$TARGET" = "$RUNNING_DEV" ]; then
    cat >&2 <<EOF
ERROR: $TARGET is the disk the currently-running system is booted from.
You cannot wipe and reinstall over yourself while it's in use.

Recovery path:
  1. Run the TUI "Wipe Disk" action to zero the GPT on $TARGET.
  2. Shut down the appliance.
  3. Power back on — with no bootable EFI on disk, the BIOS / UEFI
     firmware will fall through to the DVD with the live ISO and
     the installer can run cleanly.
EOF
    exit 1
fi

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

# -- Drop casper so the installed system boots from disk, not expecting
#    a live-mode squashfs. Without this, the installed system hits
#    /init: "Unable to find a medium containing a live file system".
chroot "$MNT" env DEBIAN_FRONTEND=noninteractive \
    apt-get -y purge casper 2>&1 | tail -3 || true

# -- Force MODULES=most so the initrd carries every available storage /
#    virt driver (hv_storvsc, virtio_scsi, nvme, ahci, ...). Module
#    auto-detection during this update-initramfs call runs in the live
#    chroot where /dev/sda is the loopback for the squashfs, not the
#    target's real SCSI/NVMe device, so MODULES=dep guesses wrong and
#    the installed system drops to the initramfs with
#    "/dev/sdaN does not exist". `most` is a small size premium for a
#    guaranteed boot on any hardware.
if [ -f "$MNT/etc/initramfs-tools/initramfs.conf" ]; then
    sed -i 's/^MODULES=.*/MODULES=most/' "$MNT/etc/initramfs-tools/initramfs.conf"
fi

# -- Rebuild initramfs now that casper's hooks are gone --
chroot "$MNT" update-initramfs -u -k all 2>&1 | tail -3

# -- Install GRUB for both firmware paths --
#
# UEFI: use --removable so grub writes to /EFI/BOOT/BOOTX64.EFI (the
# firmware fallback path) and --no-nvram so we DON'T touch the EFI
# BootOrder variable. That way:
#   * A booted live ISO / PXE / any other NVRAM entry keeps its
#     normal priority — you can always boot the live ISO for rescue
#     or reinstall without detaching the disk or deleting NVRAM
#     entries.
#   * When no removable media is present, firmware falls through to
#     the default /EFI/BOOT/BOOTX64.EFI on disk and the installed
#     system boots.
#
# BIOS: unchanged — i386-pc writes to the BIOS Boot Partition on the
# disk, no NVRAM involved.
echo "Installing GRUB..."
chroot "$MNT" grub-install --target=x86_64-efi \
    --efi-directory=/boot/efi \
    --removable --no-nvram \
    --recheck --no-floppy 2>&1 | tail -3 || true
chroot "$MNT" grub-install --target=i386-pc \
    --recheck --no-floppy "$TARGET" 2>&1 | tail -3 || true
chroot "$MNT" update-grub 2>&1 | tail -3

# -- Best-effort: delete any stale "BrainrotFilter" NVRAM entry left
#    over from earlier builds that used --bootloader-id. Harmless on
#    fresh hardware (efibootmgr prints "no match" and exits non-zero).
if chroot "$MNT" command -v efibootmgr >/dev/null 2>&1; then
    while read -r line; do
        num=$(echo "$line" | awk '{print $1}' | sed 's/^Boot//;s/\*$//')
        [ -n "$num" ] && chroot "$MNT" efibootmgr -b "$num" -B >/dev/null 2>&1 || true
    done < <(chroot "$MNT" efibootmgr 2>/dev/null | grep -E '^Boot[0-9A-F]{4}\*?\s+BrainrotFilter')
fi

# -- Make sure firmware's BootOrder keeps removable media (DVD / USB)
#    ahead of the disk. On Hyper-V Gen2 specifically, the VM's GUI-level
#    boot-order list is NOT reflected into the EFI BootOrder variable —
#    firmware silently tries the disk first even when the user set DVD
#    first in Hyper-V Manager. Force the order here so future reinstalls
#    "just work" by re-attaching the ISO and rebooting, no PowerShell or
#    detaching disks required.
if command -v efibootmgr >/dev/null 2>&1 && [ -d /sys/firmware/efi/efivars ]; then
    # Grab current entries, classify each as removable / disk / net.
    removable=""; disk=""; net=""
    while read -r line; do
        num=$(echo "$line" | sed -E 's/^Boot([0-9A-F]{4})\*?.*$/\1/')
        [ -n "$num" ] || continue
        case "$line" in
            *CDROM*|*ISO*|*DVD*|*"SCSI(0,0)"*)  removable="${removable:+$removable,}$num" ;;
            *Network*|*IPv4*|*PXE*|*MAC*)       net="${net:+$net,}$num" ;;
            *)                                   disk="${disk:+$disk,}$num" ;;
        esac
    done < <(efibootmgr 2>/dev/null | grep -E '^Boot[0-9A-F]{4}')
    # Heuristic: on Hyper-V the DVD is typically SCSI(0,0); our install
    # target is typically SCSI(0,1)+. When unsure, the "removable"
    # bucket pulls the DVD, "disk" pulls the target.
    new_order=""
    for g in "$removable" "$disk" "$net"; do
        [ -n "$g" ] && new_order="${new_order:+$new_order,}$g"
    done
    if [ -n "$new_order" ]; then
        efibootmgr -o "$new_order" >/dev/null 2>&1 || true
    fi
fi

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
