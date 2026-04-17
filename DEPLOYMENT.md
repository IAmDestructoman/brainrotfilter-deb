# BrainrotFilter — Physical Appliance Deployment Guide

This guide covers installing BrainrotFilter as a dedicated inline transparent-proxy
appliance on a physical box with 2 NICs. For VM / single-NIC test setups, use the
standard wizard single-NIC flow.

---

## 1. Hardware requirements

| Component | Minimum | Recommended |
|---|---|---|
| CPU | 2-core x86_64 | 4-core (Intel N100, J4125, N5105, or better) |
| RAM | 4 GB | 8 GB |
| Storage | 32 GB SSD (BTRFS for snapshots) | 120+ GB NVMe, BTRFS |
| NICs | 2 × 1 GbE wired | 2 × 1 GbE Intel i210/i225 or Realtek RTL8125 |
| Power | Low-power (< 20 W idle) | Fanless mini-PC (Protectli, Topton, Beelink) |

**NIC notes:**
- **Intel i210 / i225 / i350** are the most stable under Linux bridge
- Realtek RTL8111/RTL8168 sometimes need the `r8168` out-of-tree driver instead
  of the upstream `r8169` for reliability. If you see TX hangs or bridge packet
  drops under load, install `r8168-dkms`.
- **Avoid USB-Ethernet adapters** for the data path — unreliable under sustained traffic.

---

## 2. Topology

```
         ┌──────────────┐
         │ Home Router  │       (192.168.1.1, DHCP for LAN)
         └──────┬───────┘
                │                <-- your existing LAN cable
         ┌──────▼───────┐
         │  WAN port    │  eth0  <-- "upstream" facing the router
         │              │
         │ BrainrotFilter│  br0  <-- software bridge with mgmt IP 192.168.1.50
         │   Appliance  │
         │              │
         │  LAN port    │  eth1  <-- "downstream" facing the switch
         └──────┬───────┘
                │
         ┌──────▼───────┐
         │  Switch      │
         └──────┬───────┘
                │
       Client devices (PCs, phones, TVs, etc.)
```

**Key facts:**
- The appliance is a **Layer-2 bridge** — it does NOT do NAT, does NOT issue DHCP,
  does NOT become clients' default gateway.
- Clients still see the home router as their gateway; the router still hands out DHCP.
- MAC addresses pass through unchanged. Clients don't know the appliance exists.
- The appliance has exactly **one** IP on `br0` for its own admin panel + SSH (when
  enabled) access — obtained via DHCP or configured statically in the wizard.
- QUIC (UDP/443) is dropped in FORWARD to force browsers back to TCP, which is
  the only path we can intercept.

---

## 3. Install

### 3.1 Prepare the box

1. Install Ubuntu Server 24.04 LTS (minimal). During install:
   - Choose **Ubuntu Server (minimized)**, not the full server
   - Choose **BTRFS** as the filesystem for `/` — enables factory snapshots
   - Do NOT install snap, cloud-init, or any desktop
   - Create an admin user (only for initial install — you'll disable SSH after)
2. First boot: log in as admin, update:
   ```bash
   sudo apt update && sudo apt upgrade -y
   ```
3. Remove bloat (see [Hardening](#5-hardening) for the full strip list).

### 3.2 Install the .deb

```bash
sudo apt install ./brainrotfilter_1.1.0-1_all.deb
```

This pulls in: `squid`, `openssl`, `iptables`, `iptables-persistent`, `conntrack`,
`bridge-utils`, `ebtables`, `ffmpeg`, `python3-venv`, `btrfs-progs`, `snapper`,
`dialog`, `python3-httpx`.

After install:
- All BrainrotFilter services are enabled and running
- SSH is **disabled** by default (flip on in the console TUI when needed)
- Console TUI takes over `tty1` (log out of console to see it, or hit `Alt-F1`)

---

## 4. First-boot wizard

Open `http://<appliance-ip>:8199` from any device on the LAN. The wizard walks
through:

1. **Network setup**
   - Select "Transparent bridge (2 NICs inline)"
   - Pick WAN NIC + LAN NIC from the dropdowns (MAC + link state shown)
   - Configure management IP (DHCP or static)
   - Wizard writes `/etc/netplan/99-brainrotfilter-bridge.yaml` and applies
2. **CA certificate generation**
   - 4096-bit RSA CA, 10-year validity
3. **Squid + iptables**
   - SSL bump config, peek-stare-bump for YouTube, splice everything else
   - PREROUTING REDIRECT on `-i br0` for TCP 80 → 3128 and TCP 443 → 3129
   - FORWARD DROP for UDP 80/443 (QUIC)
4. **Keywords + thresholds**
   - Load defaults or import from community repo
5. **CA cert distribution**
   - Download the CA cert, install on each client device — see [Section 6](#6-ca-cert-install)

At the end of the wizard, a **BTRFS "factory" snapshot** is taken automatically.
`Console TUI → option 4 (Factory Reset)` rolls back to this state.

---

## 5. Hardening

The appliance ISO (Phase 2) automates this, but if installing on stock Ubuntu
Server, run:

```bash
# Remove snap
sudo systemctl disable --now snapd.service snapd.socket
sudo apt purge snapd -y

# Remove cloud-init
sudo touch /etc/cloud/cloud-init.disabled
sudo apt purge cloud-init -y

# Remove unused services
sudo apt purge -y \
    unattended-upgrades \
    ubuntu-advantage-tools \
    popularity-contest \
    apport whoopsie \
    motd-news-config \
    landscape-client landscape-common \
    packagekit \
    avahi-daemon \
    cups cups-browsed \
    bluez \
    modemmanager \
    wpasupplicant \
    ufw \
    plymouth plymouth-theme-spinner

# Remove any remaining docs / manuals (space + attack surface)
sudo apt purge -y man-db info
sudo rm -rf /usr/share/man/* /usr/share/doc/*

sudo apt autoremove -y
```

Then harden further:
```bash
# SSH off by default (done by postinst, but confirm):
sudo systemctl disable --now ssh.service

# /tmp noexec,nosuid,nodev:
echo 'tmpfs  /tmp  tmpfs  defaults,nosuid,nodev,noexec,size=512M  0 0' | sudo tee -a /etc/fstab

# Blacklist unused kernel modules:
sudo tee /etc/modprobe.d/brainrotfilter-blacklist.conf <<'EOF'
blacklist usb-storage
blacklist firewire-core
blacklist firewire-ohci
blacklist thunderbolt
blacklist btrfs_compress
blacklist cifs
blacklist nfs
blacklist freevxfs
blacklist jffs2
blacklist hfs
blacklist hfsplus
blacklist squashfs
blacklist udf
EOF

# Disable source routing / ICMP redirects / martian packets:
sudo tee /etc/sysctl.d/99-brainrotfilter-harden.conf <<'EOF'
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.all.log_martians = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
kernel.dmesg_restrict = 1
EOF
sudo sysctl --system
```

---

## 6. CA cert install

Every client device needs to trust the BrainrotFilter CA or it will see SSL
warnings on every YouTube page.

1. Visit `http://<appliance-ip>:8199/ca-cert` on each client device
2. Click **Download CA Certificate (.pem)**
3. Follow per-OS instructions on that page (Windows, macOS, iOS, Android,
   Chromebook, Linux all covered)

For large fleets, distribute via MDM (Jamf, Intune, Google Workspace) or
Active Directory Group Policy.

---

## 7. Post-install console TUI

Hit `Alt-F1` on the console to see the BrainrotFilter menu:

```
  0) System Status         - per-service health + recent errors
  1) Assign Interfaces     - re-run bridge setup
  2) Set Management IP     - change br0 IP
  3) Reset Admin Password  - (placeholder)
  4) Factory Reset         - snapper rollback to post-wizard snapshot
  5) Restart Services      - individual or all
  6) Update BrainrotFilter - apt upgrade
  7) Enable/Disable SSH    - toggles sshd (disabled by default)
  8) Shell                 - drops to bash (advanced)
  9) Reboot / Shutdown
```

---

## 8. Enabling ICAP enforcement (optional)

ICAP body inspection ships in **log-only mode** by default — it observes
`/youtubei/v1/(player|next)` POST bodies and logs a snapshot, but does not act on them.

To enable enforcement (queue + CDN-block decisions fired from body-extracted videoIds):

```bash
sudo systemctl edit brainrotfilter-icap
```

Add:
```ini
[Service]
Environment=BRAINROT_SHIM_ENFORCE=1
```

Save, then:
```bash
sudo systemctl restart brainrotfilter-icap
```

You can flip it back off by removing the override.

---

## 9. Troubleshooting

| Symptom | Check |
|---|---|
| Admin panel unreachable | `ip addr show br0` — confirm management IP is set |
| All YouTube traffic breaks | TUI option 5 → restart Squid. Or `journalctl -u squid -n 50` |
| Blocks don't fire | TUI option 0 → check brainrotfilter + brainrotfilter-icap active |
| Feed scroll floods Processing | Restart squid helpers (they auto-respawn) |
| Videos play at 144p | Check `delay_pool` isn't in `/etc/squid/conf.d/brainrotfilter.conf` — it was removed in 1.1.0 |
| Client device gets SSL warnings | CA cert not installed on that device — see Section 6 |
| Bridge doesn't pass traffic | `bridge link show` — confirm both NICs are members. `sysctl net.bridge.bridge-nf-call-iptables` should be 1 |

Logs for deeper debugging (enable SSH first via TUI option 7):

```bash
journalctl -u brainrotfilter -n 100
journalctl -u brainrotfilter-icap -n 100
journalctl -u brainrotfilter-watchdog -n 100
journalctl -u squid -n 100
tail -f /var/log/squid/cache.log
```
