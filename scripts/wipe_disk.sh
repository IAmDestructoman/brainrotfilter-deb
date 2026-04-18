#!/bin/bash
# wipe_disk.sh — zero the GPT / MBR on a target disk so the firmware's
# NVRAM boot entries referencing it become invalid on next power cycle.
# Invoked from the TUI "Wipe Disk" action for the case where an earlier
# install left a trap that keeps the firmware from falling through to
# the DVD drive at boot. Does NOT touch the currently-running system's
# root device — that's a refused wipe.
set -euo pipefail

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
BOLD=$'\033[1m'; NC=$'\033[0m'

die() { echo "${RED}ERROR:${NC} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root"

echo
echo "${BOLD}=== Wipe Disk ===${NC}"
echo
echo "Destroys the partition table + bootloader signatures on the target"
echo "disk so the UEFI firmware stops trying to boot from it — useful"
echo "when a failed install has trapped the VM / appliance into booting"
echo "a broken system instead of falling through to the DVD."
echo

# Find the currently-running root so we refuse to wipe it. Live boots
# have overlay on /, so RUNNING_ROOT may be empty — that's fine, nothing
# to exclude. Tests run inside `if` so set -e doesn't trip on false.
RUNNING_DEV=""
RUNNING_ROOT=$(findmnt -no SOURCE / 2>/dev/null | head -1 || true)
if [ -n "$RUNNING_ROOT" ] && [ -b "$RUNNING_ROOT" ]; then
    pk=$(lsblk -no PKNAME "$RUNNING_ROOT" 2>/dev/null | head -1 || true)
    if [ -n "$pk" ]; then
        RUNNING_DEV="/dev/$pk"
    fi
fi

# Find the live-boot medium so we refuse to wipe it too.
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

mapfile -t DISK_LINES < <(lsblk -dpno NAME,SIZE,MODEL,TYPE 2>/dev/null | awk '$NF=="disk"')
CANDIDATES=()
echo "${BOLD}Disks:${NC}"
i=0
for line in "${DISK_LINES[@]}"; do
    dev=$(awk '{print $1}' <<<"$line")
    skip=""
    if [ -n "$BOOT_DEV" ] && [ "$dev" = "$BOOT_DEV" ]; then
        skip="LIVE MEDIUM"
    elif [ -n "$RUNNING_DEV" ] && [ "$dev" = "$RUNNING_DEV" ]; then
        skip="RUNNING ROOT"
    fi
    if [ -n "$skip" ]; then
        printf '     %s  [%s - skipped]\n' "$line" "$skip"
        continue
    fi
    i=$((i+1))
    CANDIDATES+=("$dev")
    printf '  %2d) %s\n' "$i" "$line"
done
echo

if [ ${#CANDIDATES[@]} -eq 0 ]; then
    echo "${YELLOW}No wipeable disks. Every disk is either the live medium"
    echo "or the currently-running root.${NC}"
    exit 0
fi

read -rp "Pick disk to wipe (or 'q' to cancel): " PICK
if [ -z "$PICK" ] || [ "$PICK" = "q" ] || [ "$PICK" = "Q" ]; then
    echo "Cancelled."; exit 0
fi
[[ "$PICK" =~ ^[0-9]+$ ]] || die "not a number"
if [ "$PICK" -lt 1 ] || [ "$PICK" -gt "${#CANDIDATES[@]}" ]; then
    die "out of range"
fi
TARGET="${CANDIDATES[$((PICK-1))]}"

echo
echo "${RED}${BOLD}>>> $TARGET will be zeroed. Everything on it dies. <<<${NC}"
lsblk "$TARGET"
echo
read -rp "Type ${BOLD}YES${NC} (all caps) to proceed: " CONFIRM
[ "$CONFIRM" = "YES" ] || { echo "Cancelled."; exit 0; }

# Unmount / swapoff anything on the target before zapping.
for p in $(lsblk -pnlo NAME "$TARGET" | tail -n +2); do
    umount -q "$p" 2>/dev/null || true
    swapoff "$p" 2>/dev/null || true
done

echo "Zapping $TARGET..."
sgdisk --zap-all "$TARGET" 2>&1 | tail -3 || true
wipefs -a "$TARGET" 2>&1 | tail -3 || true
# Nuke the first + last 10 MiB for good measure (MBR, LVM headers,
# secondary GPT at the tail).
dd if=/dev/zero of="$TARGET" bs=1M count=10 conv=fsync 2>/dev/null || true
SIZE_BYTES=$(blockdev --getsize64 "$TARGET" 2>/dev/null || echo 0)
if [ "$SIZE_BYTES" -gt $((10*1024*1024)) ]; then
    SEEK=$(( (SIZE_BYTES / (1024*1024)) - 10 ))
    dd if=/dev/zero of="$TARGET" bs=1M count=10 seek="$SEEK" conv=fsync 2>/dev/null || true
fi

# Best-effort: drop any stale NVRAM boot entry that still points at a
# /EFI/BOOT/BOOTX64.EFI on this (now-empty) disk. If efibootmgr isn't
# available or we're on BIOS, skip.
if command -v efibootmgr >/dev/null 2>&1 && [ -d /sys/firmware/efi/efivars ]; then
    while read -r line; do
        num=$(echo "$line" | sed -E 's/^Boot([0-9A-F]{4}).*$/\1/')
        [ -n "$num" ] && efibootmgr -b "$num" -B >/dev/null 2>&1 || true
    done < <(efibootmgr 2>/dev/null | grep -E '^Boot[0-9A-F]{4}.*\b(BrainrotFilter|Linux Boot Manager|ubuntu)\b')
fi

echo
echo "${GREEN}${BOLD}Wipe complete.${NC}"
echo "Power-cycle the appliance. With no bootable signature on $TARGET,"
echo "firmware will fall through to the DVD drive / removable media."
