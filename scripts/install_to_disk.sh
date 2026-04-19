#!/bin/bash
# install_to_disk.sh - dd a pre-built bootable disk image onto the
# selected target, then expand the root partition to fill the disk.
#
# Mirrors what pfSense / OPNsense / Proxmox do: the bootable image
# is built + tested at ISO build time (iso/build_golden_image.sh),
# sealed into the ISO at /brainrot-golden.img.zst, and installation
# is a single zstd -> dd operation. No grub-install / update-grub /
# chroot tricks at install time, which removes every class of failure
# we've hit trying to build the install on the fly.
set -euo pipefail

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
BOLD=$'\033[1m'; NC=$'\033[0m'

die() { echo "${RED}ERROR:${NC} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root"

# ENABLE_UEFI: must match iso/build_golden_image.sh. When "false" we
# skip the EFI BootOrder rewrite since the golden image doesn't have
# an ESP or BOOTX64.EFI in the first place — the target boots via
# legacy BIOS from the MBR / BIOS Boot Partition.
ENABLE_UEFI="${ENABLE_UEFI:-false}"

# -- Locate the golden image. Live boot mounts the ISO at /cdrom; if
#    we're instead booted from a previous install, check /dev/sr0.
GOLDEN=""
for c in /cdrom/brainrot-golden.img.zst \
         /run/live/medium/brainrot-golden.img.zst \
         /live/image/brainrot-golden.img.zst \
         /brainrot-golden.img.zst; do
    if [ -r "$c" ]; then GOLDEN="$c"; break; fi
done

SR_MOUNT=""
if [ -z "$GOLDEN" ]; then
    for d in /dev/sr0 /dev/sr1 /dev/cdrom /dev/dvd; do
        [ -b "$d" ] || continue
        m=$(mktemp -d /tmp/brainrot-iso-src.XXXXXX)
        if mount -o ro "$d" "$m" 2>/dev/null; then
            if [ -r "$m/brainrot-golden.img.zst" ]; then
                GOLDEN="$m/brainrot-golden.img.zst"
                SR_MOUNT="$m"
                echo "Found golden image on $d"
                break
            fi
            umount "$m" 2>/dev/null || true
        fi
        rmdir "$m" 2>/dev/null || true
    done
fi

[ -n "$GOLDEN" ] || die "can't find brainrot-golden.img.zst — attach the BrainrotFilter ISO to the DVD drive and try again"

# -- Find the boot medium so we refuse to wipe it.
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
echo "Source: $GOLDEN"
if [ -n "$BOOT_DEV" ]; then
    echo "Boot media (will be skipped): $BOOT_DEV"
fi
echo

# -- List candidate disks.
mapfile -t DISK_LINES < <(lsblk -dpno NAME,SIZE,MODEL,TYPE 2>/dev/null | awk '$NF=="disk"')
CANDIDATES=()
echo "${BOLD}Available disks:${NC}"
i=0
for line in "${DISK_LINES[@]}"; do
    dev=$(awk '{print $1}' <<<"$line")
    if [ -n "$BOOT_DEV" ] && [ "$dev" = "$BOOT_DEV" ]; then
        printf '     %s  [BOOT MEDIA - skipped]\n' "$line"
        continue
    fi
    i=$((i+1))
    CANDIDATES+=("$dev")
    printf '  %2d) %s\n' "$i" "$line"
done
echo

if [ "${#CANDIDATES[@]}" -eq 0 ]; then
    die "no installable disks detected"
fi

read -rp "Pick target disk number (or 'q' to cancel): " PICK
if [ -z "$PICK" ] || [ "$PICK" = "q" ] || [ "$PICK" = "Q" ]; then
    echo "Cancelled."; exit 0
fi
[[ "$PICK" =~ ^[0-9]+$ ]] || die "not a number"
if [ "$PICK" -lt 1 ] || [ "$PICK" -gt "${#CANDIDATES[@]}" ]; then
    die "out of range"
fi
TARGET="${CANDIDATES[$((PICK-1))]}"

# Refuse if target is the currently-running root.
RUNNING_DEV=""
RUNNING_ROOT=$(findmnt -no SOURCE / 2>/dev/null | head -1 || true)
if [ -n "$RUNNING_ROOT" ] && [ -b "$RUNNING_ROOT" ]; then
    pk=$(lsblk -no PKNAME "$RUNNING_ROOT" 2>/dev/null | head -1 || true)
    if [ -n "$pk" ]; then RUNNING_DEV="/dev/$pk"; fi
fi
if [ -n "$RUNNING_DEV" ] && [ "$TARGET" = "$RUNNING_DEV" ]; then
    die "$TARGET is the currently-running root; can't install over self. Wipe first + reboot into the live ISO."
fi

echo
echo "${RED}${BOLD}>>> ALL DATA ON $TARGET WILL BE ERASED <<<${NC}"
lsblk "$TARGET"
echo
read -rp "Type ${BOLD}YES${NC} (all caps) to proceed: " CONFIRM
[ "$CONFIRM" = "YES" ] || { echo "Cancelled."; exit 0; }

# Unmount / swapoff anything still mounted on the target.
echo "Preparing target..."
for p in $(lsblk -pnlo NAME "$TARGET" | tail -n +2); do
    umount -q "$p" 2>/dev/null || true
    swapoff "$p" 2>/dev/null || true
done
wipefs -a "$TARGET" >/dev/null 2>&1 || true

# -- The dd. This is the whole install.
TGT_SIZE=$(blockdev --getsize64 "$TARGET")
IMG_SIZE_HINT="~6 GiB"
echo
echo "Writing disk image to $TARGET..."
echo "  (target: $((TGT_SIZE / 1024 / 1024 / 1024)) GiB, image: $IMG_SIZE_HINT compressed)"
echo

if command -v pv >/dev/null 2>&1; then
    zstd -dc "$GOLDEN" | pv -pterb | dd of="$TARGET" bs=4M conv=fsync status=none
else
    zstd -dc "$GOLDEN" | dd of="$TARGET" bs=4M conv=fsync status=progress
fi

sync
partprobe "$TARGET" 2>/dev/null || true
udevadm settle

# -- Fix GPT: the image ends at the image size, but the target disk
#    is almost always larger. Move the GPT backup header to the real
#    end of the disk, then grow partition 3 (root) to fill.
echo "Growing root partition to fill disk..."
sgdisk --move-second-header "$TARGET" >/dev/null 2>&1 || true
# Re-read after sgdisk.
partprobe "$TARGET" 2>/dev/null || true
udevadm settle

# parted resizepart 3 100%
parted -s "$TARGET" resizepart 3 100% 2>&1 | tail -3 || true
partprobe "$TARGET" 2>/dev/null || true
udevadm settle

# Figure out partition naming: nvme/mmc uses <dev>p<n>, everything
# else uses <dev><n>.
case "$TARGET" in
    *nvme*|*mmcblk*) P_ROOT="${TARGET}p3" ;;
    *)               P_ROOT="${TARGET}3" ;;
esac

e2fsck -fy "$P_ROOT" 2>&1 | tail -3 || true
resize2fs "$P_ROOT" 2>&1 | tail -3 || true

# -- Preserve live-session state (wizard DB, bridge config, root pw,
#    SSH state, etc.) onto the installed target. Optional — if any
#    step fails the install is still good, just missing live state.
echo "Preserving live session state..."
MNT=$(mktemp -d /tmp/brainrot-install.XXXXXX)
if mount "$P_ROOT" "$MNT" 2>/dev/null; then
    for src in /etc/brainrotfilter \
               /var/lib/brainrotfilter \
               /etc/systemd/network \
               /etc/netplan \
               /etc/sudoers.d \
               /etc/iptables \
               /etc/ssh/sshd_config.d; do
        if [ -d "$src" ] && [ -n "$(ls -A "$src" 2>/dev/null)" ]; then
            mkdir -p "$MNT$src"
            rsync -a "$src/" "$MNT$src/" 2>/dev/null || true
        fi
    done
    for f in /etc/passwd /etc/shadow /etc/group /etc/gshadow; do
        if [ -f "$f" ]; then
            cp -a "$f" "$MNT$f" 2>/dev/null || true
        fi
    done
    for wants in /etc/systemd/system/multi-user.target.wants \
                 /etc/systemd/system/sockets.target.wants \
                 /etc/systemd/system/network-pre.target.wants; do
        if [ -d "$wants" ]; then
            mkdir -p "$MNT$wants"
            rsync -a "$wants/" "$MNT$wants/" 2>/dev/null || true
        fi
    done
    find /etc/systemd/system -maxdepth 1 -type l 2>/dev/null | while read -r ln; do
        cp -a "$ln" "$MNT$ln" 2>/dev/null || true
    done
    # Wipe host keys so each installed box regenerates unique ones on
    # first SSH-enable.
    rm -f "$MNT"/etc/ssh/ssh_host_*_key "$MNT"/etc/ssh/ssh_host_*_key.pub 2>/dev/null || true
    sync
    umount "$MNT"
fi
rmdir "$MNT" 2>/dev/null || true

# -- Ensure firmware's BootOrder keeps removable media ahead of the
#    disk. On Hyper-V Gen2 the VM-GUI boot-order list is advisory; the
#    real BootOrder NVRAM variable can put the disk first by default
#    and trap the firmware there, defeating later reinstalls.
if [ "$ENABLE_UEFI" = "true" ] && command -v efibootmgr >/dev/null 2>&1 && [ -d /sys/firmware/efi/efivars ]; then
    removable=""; disk_boot=""; net=""
    while read -r line; do
        num=$(echo "$line" | sed -E 's/^Boot([0-9A-F]{4})\*?.*$/\1/')
        [ -n "$num" ] || continue
        case "$line" in
            *CDROM*|*ISO*|*DVD*|*"SCSI(0,0)"*)  removable="${removable:+$removable,}$num" ;;
            *Network*|*IPv4*|*PXE*|*MAC*)       net="${net:+$net,}$num" ;;
            *)                                   disk_boot="${disk_boot:+$disk_boot,}$num" ;;
        esac
    done < <(efibootmgr 2>/dev/null | grep -E '^Boot[0-9A-F]{4}')
    new_order=""
    for g in "$removable" "$disk_boot" "$net"; do
        if [ -n "$g" ]; then
            new_order="${new_order:+$new_order,}$g"
        fi
    done
    if [ -n "$new_order" ]; then
        efibootmgr -o "$new_order" >/dev/null 2>&1 || true
    fi
fi

# -- Unmount our ISO helper mount if we created one.
if [ -n "$SR_MOUNT" ]; then
    umount "$SR_MOUNT" 2>/dev/null || true
    rmdir "$SR_MOUNT" 2>/dev/null || true
fi

sync

echo
echo "${GREEN}${BOLD}Install complete.${NC}"
echo "Target: $TARGET"
echo "Reboot and remove the live media. The installed system will boot"
echo "from $TARGET."
