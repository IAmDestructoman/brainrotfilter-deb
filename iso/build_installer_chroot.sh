#!/bin/bash
# Build a minimal installer-only chroot. Its only purpose is to boot
# enough Linux to run install_to_disk.sh (dd the golden image to the
# target). No BrainrotFilter service, no wizard, no venv — that's all
# in the golden image, which becomes the installed system.
#
# Matches the pfSense / OPNsense pattern: installer is tiny, the
# sealed disk image is what actually gets deployed.
#
# Usage:
#   build_installer_chroot.sh <out-dir>
set -euo pipefail

OUT="${1:?output chroot dir required}"
MIRROR="${MIRROR:-http://archive.ubuntu.com/ubuntu}"
SUITE="${SUITE:-noble}"

echo "[installer-chroot] debootstrap $SUITE into $OUT..."
rm -rf "$OUT"
mkdir -p "$OUT"
debootstrap --variant=minbase --arch=amd64 "$SUITE" "$OUT" "$MIRROR"

# --- Enable universe for casper ---
cat > "$OUT/etc/apt/sources.list" <<EOF
deb $MIRROR $SUITE main universe
deb $MIRROR $SUITE-updates main universe
EOF

mount --bind /dev     "$OUT/dev"
mount --bind /dev/pts "$OUT/dev/pts"
mount -t proc  proc   "$OUT/proc"
mount -t sysfs sys    "$OUT/sys"

trap '
    umount -l "$OUT/sys"     2>/dev/null || true
    umount -l "$OUT/proc"    2>/dev/null || true
    umount -l "$OUT/dev/pts" 2>/dev/null || true
    umount -l "$OUT/dev"     2>/dev/null || true
' EXIT

echo "[installer-chroot] Installing packages..."
chroot "$OUT" env DEBIAN_FRONTEND=noninteractive apt-get update
chroot "$OUT" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    linux-image-generic \
    linux-firmware \
    initramfs-tools \
    casper \
    systemd systemd-sysv dbus udev \
    util-linux coreutils bash \
    iproute2 iputils-ping iproute2 \
    openssh-server \
    sudo \
    python3 python3-minimal \
    ncurses-base ncurses-bin \
    zstd pv \
    parted gdisk dosfstools e2fsprogs \
    rsync \
    efibootmgr \
    ca-certificates \
    kbd console-setup \
    less nano

# --- Strip caches ---
chroot "$OUT" apt-get clean
rm -rf "$OUT/var/lib/apt/lists"/* "$OUT/var/cache/apt/archives"/*.deb
rm -rf "$OUT/usr/share/doc"/* "$OUT/usr/share/man"/*
find "$OUT/usr/share/locale" -maxdepth 1 -mindepth 1 -type d \
    ! -name 'en*' -exec rm -rf {} + 2>/dev/null || true

# --- Create appliance user; TUI is its shell ---
chroot "$OUT" useradd -m -s /usr/lib/brainrotfilter/tui/console_tui.py appliance || true
chroot "$OUT" passwd -d appliance || true
chroot "$OUT" usermod -aG sudo appliance

# --- Sudoers NOPASSWD for appliance, !use_pty so TUI reboot works ---
mkdir -p "$OUT/etc/sudoers.d"
cat > "$OUT/etc/sudoers.d/50-brainrotfilter-appliance" <<'EOF'
Defaults:appliance !use_pty
appliance ALL=(ALL) NOPASSWD: ALL
EOF
chmod 0440 "$OUT/etc/sudoers.d/50-brainrotfilter-appliance"

# --- Autologin on tty1 + tty2 debug console ---
mkdir -p "$OUT/etc/systemd/system/getty@tty1.service.d"
cat > "$OUT/etc/systemd/system/getty@tty1.service.d/autologin.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty -o '-p -- appliance' --noclear --autologin appliance %I $TERM
EOF

# --- Drop our TUI + installer scripts into the chroot ---
mkdir -p "$OUT/usr/lib/brainrotfilter/tui" "$OUT/usr/lib/brainrotfilter/scripts"
cp /mnt/e/Code/brainrotfilter-deb/src/brainrotfilter/console_tui.py \
   "$OUT/usr/lib/brainrotfilter/tui/console_tui.py"
chmod +x "$OUT/usr/lib/brainrotfilter/tui/console_tui.py"

for s in install_to_disk.sh wipe_disk.sh; do
    cp "/mnt/e/Code/brainrotfilter-deb/scripts/$s" \
       "$OUT/usr/lib/brainrotfilter/scripts/$s"
    chmod +x "$OUT/usr/lib/brainrotfilter/scripts/$s"
done

# --- Hostname + basic host files ---
echo "brainrot-installer" > "$OUT/etc/hostname"
cat > "$OUT/etc/hosts" <<EOF
127.0.0.1 localhost brainrot-installer
::1       localhost ip6-localhost ip6-loopback
EOF

# --- Mask SSH so it doesn't start by default (same as harden hook) ---
chroot "$OUT" systemctl disable ssh.service 2>/dev/null || true
chroot "$OUT" systemctl mask ssh.service ssh.socket 2>/dev/null || true

# --- Firstboot per-NIC DHCP so the installer has network if needed ---
mkdir -p "$OUT/etc/netplan"
cat > "$OUT/etc/netplan/00-brainrot-firstboot.yaml" <<'EOF'
network:
  version: 2
  renderer: networkd
  ethernets:
    all-en:
      match:
        name: "en*"
      dhcp4: true
      dhcp6: false
      optional: true
    all-eth:
      match:
        name: "eth*"
      dhcp4: true
      dhcp6: false
      optional: true
EOF
chmod 600 "$OUT/etc/netplan/00-brainrot-firstboot.yaml"
chroot "$OUT" systemctl enable systemd-networkd.service 2>/dev/null || true

# --- MODULES=most so initramfs carries every storage driver ---
if [ -f "$OUT/etc/initramfs-tools/initramfs.conf" ]; then
    sed -i 's/^MODULES=.*/MODULES=most/' "$OUT/etc/initramfs-tools/initramfs.conf"
fi
chroot "$OUT" update-initramfs -u -k all 2>&1 | tail -3

umount -l "$OUT/sys"     2>/dev/null || true
umount -l "$OUT/proc"    2>/dev/null || true
umount -l "$OUT/dev/pts" 2>/dev/null || true
umount -l "$OUT/dev"     2>/dev/null || true
trap - EXIT

du -sh "$OUT"
echo "[installer-chroot] Done: $OUT"
