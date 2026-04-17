# BrainrotFilter Appliance ISO Build

This directory contains the build pipeline for the BrainrotFilter custom
Ubuntu Server ISO — a bootable installer that produces a hardened,
minimal appliance image with the BrainrotFilter `.deb` pre-installed.

## Build pipeline

Uses **live-build** (`lb_config`, `lb_build`) to customize a stock
Ubuntu Server 24.04 LTS base into a stripped-down appliance.

### Prerequisites

Build the ISO on an Ubuntu 22.04+ host:

```bash
sudo apt install live-build xorriso debian-archive-keyring
```

### Quick start

1. Build the .deb first (run from repo root):
   ```bash
   dpkg-buildpackage -us -uc -b
   ```

2. Drop the resulting `brainrotfilter_*.deb` into `iso/packages/`:
   ```bash
   cp ../brainrotfilter_*.deb iso/packages/
   ```

3. Build the ISO:
   ```bash
   cd iso/
   sudo ./build.sh
   ```

4. Output: `iso/brainrotfilter-1.1.0.iso` — flash to USB with
   `dd` / Balena Etcher / Rufus and boot target hardware.

## Structure

```
iso/
├── README.md               — this file
├── build.sh                — main build driver
├── config/
│   ├── package-lists/
│   │   ├── base.list.chroot       — minimal base packages
│   │   └── brainrot.list.chroot   — our runtime deps
│   ├── package-lists.purge/       — packages to REMOVE from base
│   ├── includes.chroot/           — files added to the installed system
│   │   ├── etc/
│   │   │   ├── motd
│   │   │   ├── issue
│   │   │   ├── modprobe.d/brainrotfilter-blacklist.conf
│   │   │   ├── sysctl.d/99-brainrotfilter-harden.conf
│   │   │   └── systemd/system/getty@tty1.service.d/autologin.conf
│   │   └── usr/local/sbin/brainrot-firstboot.sh
│   └── hooks/
│       ├── 0010-strip-bloat.hook.chroot
│       ├── 0020-disable-services.hook.chroot
│       └── 0030-install-brainrotfilter.hook.chroot
└── packages/
    └── brainrotfilter_*.deb     — the .deb to preinstall (dropped here before build)
```

## What the ISO provides

- **Base:** Ubuntu Server 24.04 LTS, kernel only, no desktop/snap/cloud-init
- **Filesystem:** BTRFS root (enables factory snapshot + rollback)
- **Pre-installed:** `squid`, `iptables-persistent`, `bridge-utils`, `ebtables`,
  `conntrack`, `ffmpeg`, `python3-venv`, `btrfs-progs`, `snapper`, `dialog`,
  `openssl`, and the BrainrotFilter `.deb`
- **Removed from base:** snap, cloud-init, unattended-upgrades, apport,
  popularity-contest, ubuntu-advantage-tools, avahi, cups, bluetooth,
  wpasupplicant, NetworkManager, ufw, man-db, info, plymouth, modemmanager,
  packagekit
- **Console:** auto-login to tty1 → BrainrotFilter TUI menu (pfSense-style)
- **SSH:** disabled by default (toggle in TUI menu option 7)
- **First-boot:** creates initial BTRFS "factory" snapshot after wizard completes
- **Login:** single `admin` user (password set on first boot via wizard)

## Notes

- Build takes ~15-25 minutes depending on host network speed
- ISO size target: **< 800 MB** (vs 2.5 GB for default Ubuntu Server)
- Image is hybrid UEFI + BIOS boot
- No pre-seeded passwords — wizard generates on first boot
