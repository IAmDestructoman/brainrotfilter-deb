#!/usr/bin/env python3
"""
BrainrotFilter Console TUI — pfSense-style management menu.

Runs on tty1 as the auto-login shell, providing a numbered menu for
system management without requiring SSH.

Pure Python 3.9+, no external dependencies beyond stdlib.
"""

import os
import signal
import subprocess
import sys
import textwrap
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICES = [
    ("brainrotfilter", "BrainrotFilter Core"),
    ("brainrotfilter-icap", "ICAP Server"),
    ("squid", "Squid Proxy"),
    ("brainrotfilter-watchdog", "Watchdog"),
]

BRIDGE_IFACE = "br0"
WEB_PORT = 8199

# ---------------------------------------------------------------------------
# Helpers — subprocess wrappers
# ---------------------------------------------------------------------------


def _sudo(cmd: List[str]) -> List[str]:
    """Prefix cmd with `sudo -n` when not running as root.

    The TUI runs as the unprivileged `appliance` user via tty1 autologin,
    but many management actions (netplan write + apply, systemctl
    restart/reboot, snapper rollback, SSH toggle) need root. A sudoers
    drop-in (installed by the appliance harden hook) grants
    `appliance ALL=(ALL) NOPASSWD: ALL` so `sudo -n` works without a prompt.
    """
    if os.geteuid() == 0:
        return cmd
    return ["sudo", "-n", *cmd]


def _run(cmd: List[str], *, timeout: int = 15, capture: bool = True, sudo: bool = False) -> subprocess.CompletedProcess:
    """Run a command, swallowing errors so the TUI never crashes.

    Set sudo=True for commands that require root; the helper prepends
    `sudo -n` when the TUI isn't already root.
    """
    if sudo:
        cmd = _sudo(cmd)
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, stdout="", stderr="command timed out")
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=str(exc))


def _format_web_url(mgmt_ip: str) -> str:
    """Format the Web UI URL; returns a placeholder when IP is unset.

    N.B. check the raw string before stripping the CIDR suffix — "N/A"
    split on '/' would leave "N" and the placeholder check misfires.
    """
    if not mgmt_ip or mgmt_ip == "N/A":
        return "(set Management IP first)"
    host = mgmt_ip.split("/")[0].strip()
    if not host:
        return "(set Management IP first)"
    return f"http://{host}:{WEB_PORT}"


def _run_interactive(cmd: List[str], *, timeout: int = 300) -> int:
    """Run a command with inherited stdio (interactive)."""
    try:
        result = subprocess.run(cmd, timeout=timeout)
        return result.returncode
    except Exception as exc:
        print(f"  Error: {exc}")
        return 1


# ---------------------------------------------------------------------------
# System queries
# ---------------------------------------------------------------------------


def get_uptime() -> str:
    r = _run(["uptime", "-p"])
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def get_management_ip() -> str:
    """Return the first IPv4 address on br0, falling back to the default-route iface."""
    # Try br0 first
    r = _run(["ip", "-4", "-o", "addr", "show", "dev", BRIDGE_IFACE])
    if r.returncode == 0 and r.stdout.strip():
        for line in r.stdout.strip().splitlines():
            parts = line.split()
            for i, tok in enumerate(parts):
                if tok == "inet" and i + 1 < len(parts):
                    return parts[i + 1]  # includes /prefix

    # Fallback: first address on the default-route interface
    r = _run(["ip", "-4", "route", "show", "default"])
    if r.returncode == 0 and r.stdout.strip():
        parts = r.stdout.strip().split()
        if "dev" in parts:
            dev = parts[parts.index("dev") + 1]
            r2 = _run(["ip", "-4", "-o", "addr", "show", "dev", dev])
            if r2.returncode == 0:
                for line in r2.stdout.strip().splitlines():
                    p = line.split()
                    for i, tok in enumerate(p):
                        if tok == "inet" and i + 1 < len(p):
                            return p[i + 1]
    return "N/A"


def get_bridge_info() -> Tuple[str, List[str]]:
    """Return (bridge_state, [member_nics])."""
    r = _run(["ip", "-o", "link", "show", "dev", BRIDGE_IFACE])
    if r.returncode != 0 or not r.stdout.strip():
        return ("DOWN", [])

    state = "UP" if "state UP" in r.stdout else "DOWN"

    members: List[str] = []
    r2 = _run(["bridge", "link", "show"])
    if r2.returncode == 0:
        for line in r2.stdout.strip().splitlines():
            if f"master {BRIDGE_IFACE}" in line:
                parts = line.split()
                # format: "N: ethX: <FLAGS> ..."
                for p in parts:
                    if p.endswith(":") and p[:-1] not in ("", BRIDGE_IFACE):
                        name = p.rstrip(":")
                        if name and not name.isdigit():
                            members.append(name)
                            break
    return (state, members)


def get_service_state(name: str) -> str:
    """Return active/inactive/failed/unknown for a systemd unit."""
    r = _run(["systemctl", "is-active", name])
    return r.stdout.strip() if r.returncode in (0, 3) else "unknown"


def get_all_service_states() -> Dict[str, str]:
    return {svc: get_service_state(svc) for svc, _ in SERVICES}


def get_ssh_state() -> str:
    return get_service_state("ssh")


def get_ssh_fingerprints() -> str:
    r = _run(["ssh-keygen", "-l", "-f", "/etc/ssh/ssh_host_ed25519_key.pub"])
    if r.returncode == 0:
        return r.stdout.strip()
    r = _run(["ssh-keygen", "-l", "-f", "/etc/ssh/ssh_host_rsa_key.pub"])
    return r.stdout.strip() if r.returncode == 0 else "unavailable"


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def _state_color(state: str) -> str:
    if state == "active":
        return f"{GREEN}{state}{RESET}"
    elif state == "failed":
        return f"{RED}{state}{RESET}"
    elif state == "inactive":
        return f"{YELLOW}{state}{RESET}"
    return state


def clear_screen() -> None:
    # ANSI clear + cursor-home: works on every terminal the TUI will run on
    # (Linux tty1, SSH xterm) and doesn't shell out.
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def pause() -> None:
    print()
    try:
        input("  Press Enter to continue...")
    except EOFError:
        pass


def confirm(prompt: str) -> bool:
    try:
        ans = input(f"  {prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


def _wizard_complete() -> bool:
    """True once the admin wizard has run. Matches brainrotfilter.wizard_integration
    which writes /etc/brainrotfilter/.wizard_complete on success.
    """
    return os.path.exists("/etc/brainrotfilter/.wizard_complete")


def print_header() -> None:
    br_state, br_members = get_bridge_info()
    mgmt_ip = get_management_ip()
    uptime = get_uptime()
    states = get_all_service_states()
    ssh = get_ssh_state()

    width = 72
    sep = "=" * width

    print(f"\n{BOLD}{CYAN}{sep}{RESET}")
    print(f"{BOLD}{CYAN}  BrainrotFilter Appliance Console{RESET}")
    print(f"{CYAN}{sep}{RESET}")
    print()

    # Setup-pending banner. First-boot shows DHCP-assigned IP via the
    # firstboot netplan; point the operator at the web wizard and
    # highlight that bridge mode is not yet active.
    if not _wizard_complete():
        url = _format_web_url(mgmt_ip)
        print(f"  {YELLOW}{BOLD}[ Setup not complete — open {url} to finish configuration ]{RESET}")
        print()

    # Network
    if br_members:
        members_str = ", ".join(br_members)
        print(f"  Bridge {BRIDGE_IFACE}: {_state_color(br_state.lower())}  members: {members_str}")
    else:
        print(f"  Bridge {BRIDGE_IFACE}: {_state_color(br_state.lower())}  (no members detected)")
    print(f"  Management IP: {BOLD}{mgmt_ip}{RESET}")
    print(f"  Web UI:        {_format_web_url(mgmt_ip)}")
    print(f"  Uptime:        {uptime}")
    print()

    # Services — compact line
    svc_parts = []
    for svc, label in SERVICES:
        svc_parts.append(f"{label}: {_state_color(states.get(svc, 'unknown'))}")
    svc_parts.append(f"SSH: {_state_color(ssh)}")
    print(f"  {' | '.join(svc_parts)}")

    print(f"\n{CYAN}{'-' * width}{RESET}")


# ---------------------------------------------------------------------------
# Menu actions
# ---------------------------------------------------------------------------


def action_system_status() -> None:
    """0) System Status"""
    clear_screen()
    print(f"\n{BOLD}  === System Status ==={RESET}\n")

    states = get_all_service_states()
    for svc, label in SERVICES:
        state = states.get(svc, "unknown")
        print(f"  {label:<30s} {_state_color(state)}")

    ssh = get_ssh_state()
    print(f"  {'SSH':<30s} {_state_color(ssh)}")

    print(f"\n{BOLD}  --- Recent errors (last 20 lines) ---{RESET}\n")
    svc_names = [s for s, _ in SERVICES]
    r = _run(
        ["journalctl", "-u", " -u ".join(svc_names).replace(" ", "").split()]
        if False else
        ["journalctl", "--no-pager", "-p", "err", "-n", "20",
         "--unit=brainrotfilter",
         "--unit=brainrotfilter-icap",
         "--unit=squid",
         "--unit=brainrotfilter-watchdog"],
        timeout=10,
    )
    if r.returncode == 0 and r.stdout.strip():
        for line in r.stdout.strip().splitlines():
            print(f"  {line}")
    else:
        print(f"  {GREEN}No recent errors.{RESET}")

    pause()


def _list_physical_nics() -> List[Dict[str, str]]:
    """Enumerate physical ethernet NICs with state, carrier, speed, addr, MAC."""
    out: List[Dict[str, str]] = []
    r = _run(["ls", "/sys/class/net"])
    names = (r.stdout or "").split()
    for n in sorted(names):
        if n == "lo" or n.startswith(("br", "veth", "docker", "tun", "tap", "wg", "virbr")):
            continue
        # Must have a device (excludes pure virtual).
        rc = _run(["test", "-e", f"/sys/class/net/{n}/device"])
        if rc.returncode != 0:
            continue

        info: Dict[str, str] = {"name": n, "state": "?", "carrier": "?",
                                "speed": "?", "addr": "", "mac": ""}
        try:
            with open(f"/sys/class/net/{n}/operstate") as f:
                info["state"] = f.read().strip()
        except Exception:
            pass
        try:
            with open(f"/sys/class/net/{n}/carrier") as f:
                info["carrier"] = "up" if f.read().strip() == "1" else "down"
        except Exception:
            info["carrier"] = "down"
        try:
            with open(f"/sys/class/net/{n}/speed") as f:
                s = f.read().strip()
                info["speed"] = f"{s}Mb" if s and s != "-1" else "-"
        except Exception:
            info["speed"] = "-"
        try:
            with open(f"/sys/class/net/{n}/address") as f:
                info["mac"] = f.read().strip()
        except Exception:
            pass
        # IPv4 address
        ra = _run(["ip", "-4", "-o", "addr", "show", "dev", n])
        for line in (ra.stdout or "").splitlines():
            parts = line.split()
            if "inet" in parts:
                i = parts.index("inet")
                if i + 1 < len(parts):
                    info["addr"] = parts[i + 1]
                    break
        out.append(info)
    return out


def _tui_create_bridge(nics: List[Dict[str, str]]) -> None:
    if len(nics) < 2:
        print(f"  {RED}Need at least 2 physical NICs to create a bridge.{RESET}")
        return
    print()
    for i, n in enumerate(nics, 1):
        carrier = f"{GREEN}up{RESET}" if n["carrier"] == "up" else f"{YELLOW}down{RESET}"
        print(f"    {i}) {n['name']:<12}  link={carrier}  addr={n['addr'] or '-'}")
    print()
    try:
        picks = input("  NICs for br0 (comma-separated numbers, e.g. 1,2): ").strip()
    except EOFError:
        return
    try:
        indices = [int(p.strip()) - 1 for p in picks.split(",") if p.strip()]
    except ValueError:
        print(f"  {RED}Invalid selection.{RESET}")
        return
    if len(indices) < 2 or any(i < 0 or i >= len(nics) for i in indices):
        print(f"  {RED}Pick at least 2 valid NICs.{RESET}")
        return
    members = [nics[i]["name"] for i in indices]
    print(f"\n  Will create br0 across: {', '.join(members)}")
    print("  br0 will DHCP on the combined L2 segment.")
    if not confirm("Create bridge?"):
        return

    # Write systemd-networkd config. 20-brainrot-bridge-members.network
    # enumerates the chosen members explicitly (not a wildcard), so a 3rd
    # NIC that might be plugged in later won't get pulled in by accident.
    netdev = (
        "[NetDev]\nName=br0\nKind=bridge\n\n"
        "[Bridge]\nSTP=no\nForwardDelaySec=0\n"
    )
    match_names = " ".join(members)
    members_net = (
        f"[Match]\nName={match_names}\nType=ether\n\n"
        "[Network]\nBridge=br0\n"
    )
    br0_net = (
        "[Match]\nName=br0\n\n"
        "[Network]\nDHCP=ipv4\nLinkLocalAddressing=ipv6\n"
    )
    for path, content in (
        ("/etc/systemd/network/10-brainrot-br0.netdev", netdev),
        ("/etc/systemd/network/20-brainrot-bridge-members.network", members_net),
        ("/etc/systemd/network/30-brainrot-br0.network", br0_net),
    ):
        p = subprocess.run(_sudo(["tee", path]), input=content,
                           capture_output=True, text=True)
        if p.returncode != 0:
            print(f"  {RED}Failed to write {path}: {p.stderr.strip()}{RESET}")
            return

    # Remove the firstboot DHCP-all netplan so it can't fight with br0.
    _run(["rm", "-f", "/etc/netplan/00-brainrot-firstboot.yaml"], sudo=True)

    print("  Applying...")
    _run(["netplan", "apply"], timeout=20, sudo=True)
    _run(["networkctl", "reload"], timeout=10, sudo=True)
    _run(["networkctl", "reconfigure", "br0"], timeout=10, sudo=True)
    print(f"  {GREEN}br0 created. It may take a moment to DHCP.{RESET}")


def _tui_destroy_bridge() -> None:
    r = _run(["ip", "link", "show", "dev", BRIDGE_IFACE])
    if r.returncode != 0:
        print(f"  {YELLOW}br0 does not exist.{RESET}")
        return
    if not confirm("Destroy br0? All traffic through the bridge will drop"):
        return
    # Remove our systemd-networkd files + any legacy netplan bridge yaml.
    for path in (
        "/etc/systemd/network/10-brainrot-br0.netdev",
        "/etc/systemd/network/20-brainrot-bridge-members.network",
        "/etc/systemd/network/30-brainrot-br0.network",
        "/etc/netplan/99-brainrotfilter-bridge.yaml",
    ):
        _run(["rm", "-f", path], sudo=True)
    _run(["ip", "link", "set", "br0", "down"], sudo=True)
    _run(["ip", "link", "del", "br0"], sudo=True)
    _run(["networkctl", "reload"], sudo=True)
    print(f"  {GREEN}br0 destroyed. Physical NICs will DHCP independently "
          f"again after reload.{RESET}")


def action_assign_interfaces() -> None:
    """1) Assign Interfaces"""
    while True:
        clear_screen()
        print(f"\n{BOLD}  === Network Interfaces ==={RESET}\n")

        nics = _list_physical_nics()
        if not nics:
            print(f"  {YELLOW}No physical ethernet interfaces detected.{RESET}")
            pause()
            return

        print(f"  {'NAME':<12} {'STATE':<8} {'LINK':<6} {'SPEED':<8} {'ADDRESS':<18} MAC")
        print(f"  {'-' * 70}")
        for n in nics:
            carrier_col = GREEN if n["carrier"] == "up" else YELLOW
            addr = n["addr"] or "-"
            print(f"  {n['name']:<12} {n['state']:<8} "
                  f"{carrier_col}{n['carrier']:<6}{RESET} "
                  f"{n['speed']:<8} {addr:<18} {n['mac']}")

        br_state, br_members = get_bridge_info()
        print()
        if br_members:
            print(f"  Bridge {BRIDGE_IFACE}: {_state_color(br_state.lower())}  "
                  f"members: {', '.join(br_members)}")
        else:
            print(f"  Bridge {BRIDGE_IFACE}: {_state_color(br_state.lower())}  "
                  f"(not configured)")

        print()
        print("    1) Create bridge (br0) from 2+ NICs")
        print("    2) Destroy bridge (br0)")
        print("    3) Refresh")
        print("    0) Back to main menu")
        print()

        try:
            choice = input("  Select: ").strip()
        except EOFError:
            return

        if choice == "0" or choice == "":
            return
        if choice == "1":
            _tui_create_bridge(nics)
            pause()
        elif choice == "2":
            _tui_destroy_bridge()
            pause()
        elif choice == "3":
            continue
        else:
            print(f"  {RED}Invalid selection.{RESET}")
            pause()


def action_set_management_ip() -> None:
    """2) Set Management IP"""
    clear_screen()
    print(f"\n{BOLD}  === Set Management IP ==={RESET}\n")
    current = get_management_ip()
    print(f"  Current: {current}\n")

    try:
        ip_cidr = input("  Enter new IP/prefix (e.g. 192.168.1.1/24): ").strip()
        if not ip_cidr or "/" not in ip_cidr:
            print("  Cancelled — invalid format (need IP/prefix).")
            pause()
            return

        gateway = input("  Gateway (e.g. 192.168.1.1, or blank to skip): ").strip()

        dns = input("  DNS server (e.g. 1.1.1.1, or blank for 1.1.1.1): ").strip()
        if not dns:
            dns = "1.1.1.1"

    except EOFError:
        print("\n  Cancelled.")
        pause()
        return

    # Determine which interface to configure. On a fresh appliance the
    # wizard hasn't run yet so br0 doesn't exist — we configure whichever
    # physical NIC currently owns the default route (the DHCP'd one).
    # Once the wizard has created br0, the default route lives on br0
    # and we configure that.
    iface = BRIDGE_IFACE
    is_bridge = False
    r = _run(["ip", "-d", "link", "show", "dev", BRIDGE_IFACE])
    if r.returncode == 0:
        is_bridge = "bridge" in r.stdout.lower()
    else:
        r2 = _run(["ip", "-4", "route", "show", "default"])
        if r2.returncode == 0 and "dev" in r2.stdout:
            parts = r2.stdout.strip().split()
            iface = parts[parts.index("dev") + 1]
        else:
            # No default route and no br0 — enumerate physical NICs and
            # prompt the operator.
            r3 = _run(["ls", "/sys/class/net"])
            cands = []
            for n in (r3.stdout or "").split():
                if n in ("lo",) or n.startswith(("br", "veth", "docker", "tun", "tap", "wg", "virbr")):
                    continue
                cands.append(n)
            if len(cands) == 1:
                iface = cands[0]
            elif cands:
                print("  Physical interfaces detected:")
                for i, n in enumerate(cands, 1):
                    print(f"    {i}) {n}")
                try:
                    pick = input("  Pick interface number to configure: ").strip()
                    idx = int(pick) - 1
                    if 0 <= idx < len(cands):
                        iface = cands[idx]
                except (EOFError, ValueError):
                    print("  Cancelled.")
                    pause()
                    return

    # Build a systemd-networkd .network file (INI format).
    lines = [
        "# BrainrotFilter management network — written by console TUI",
        "[Match]",
        f"Name={iface}",
        "",
        "[Network]",
        f"Address={ip_cidr}",
        f"DNS={dns}",
    ]
    if gateway:
        lines.append(f"Gateway={gateway}")
    netplan_yaml = "\n".join(lines) + "\n"

    print(f"\n  Will write the following netplan config for {iface}:\n")
    for line in netplan_yaml.splitlines():
        print(f"    {line}")
    print()

    if not confirm("Apply this configuration?"):
        print("  Cancelled.")
        pause()
        return

    # Write directly to the systemd-networkd file that owns this iface.
    # For br0 that's the one installed by the ISO harden hook; rewriting
    # it in place keeps the bridge membership file (20-brainrot-bridge-
    # members.network) untouched, so physical NICs remain enslaved.
    if is_bridge and iface == BRIDGE_IFACE:
        net_path = "/etc/systemd/network/30-brainrot-br0.network"
    else:
        net_path = f"/etc/systemd/network/80-brainrot-mgmt-{iface}.network"

    proc = subprocess.run(
        _sudo(["tee", net_path]),
        input=netplan_yaml,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"  {RED}Failed to write network config: {proc.stderr.strip()}{RESET}")
        pause()
        return

    # Drop stale netplan mgmt file from earlier 1.1.0 builds that wrote
    # to /etc/netplan/90-brainrotfilter-mgmt.yaml — now dead code path.
    _run(["rm", "-f", "/etc/netplan/90-brainrotfilter-mgmt.yaml"], sudo=True)

    # Apply: reload networkd so it picks up the new config, then
    # reconfigure the target interface (faster than a full restart).
    print("  Applying...")
    r = _run(["networkctl", "reload"], timeout=15, sudo=True)
    if r.returncode == 0:
        _run(["networkctl", "reconfigure", iface], timeout=15, sudo=True)
        print(f"  {GREEN}Network configuration applied.{RESET}")
    else:
        print(f"  {RED}networkctl reload failed: {r.stderr.strip()}{RESET}")

    pause()


def action_reset_admin_password() -> None:
    """3) Reset Admin Password"""
    clear_screen()
    print(f"\n{BOLD}  === Reset Admin Password ==={RESET}\n")
    print(f"  {YELLOW}Coming soon.{RESET}")
    print(f"  This feature will allow resetting the web UI admin password.")
    pause()


def action_factory_reset() -> None:
    """4) Factory Reset"""
    clear_screen()
    print(f"\n{BOLD}  === Factory Reset ==={RESET}\n")
    print(f"  {RED}WARNING: This will revert the system to the initial snapshot{RESET}")
    print(f"  {RED}using 'snapper rollback 1'. All changes will be lost.{RESET}\n")

    if not confirm("Are you SURE you want to factory-reset?"):
        print("  Cancelled.")
        pause()
        return

    if not confirm("FINAL WARNING — type 'y' again to confirm factory reset"):
        print("  Cancelled.")
        pause()
        return

    print("\n  Performing factory reset...")
    r = _run(["snapper", "rollback", "1"], timeout=120, sudo=True)
    if r.returncode != 0:
        print(f"  {RED}snapper rollback failed: {r.stderr.strip()}{RESET}")
        pause()
        return

    print(f"  {GREEN}Rollback complete. Rebooting...{RESET}")
    _run(["systemctl", "reboot"], sudo=True)


def action_restart_services() -> None:
    """5) Restart Services"""
    clear_screen()
    print(f"\n{BOLD}  === Restart Services ==={RESET}\n")

    for i, (svc, label) in enumerate(SERVICES):
        state = get_service_state(svc)
        print(f"    {i + 1}) {label:<30s} [{_state_color(state)}]")
    print(f"    {len(SERVICES) + 1}) {'All services':<30s}")
    print(f"    0) Cancel")
    print()

    try:
        choice = input("  Select: ").strip()
    except EOFError:
        return

    if choice == "0":
        return

    targets: List[str] = []
    try:
        idx = int(choice)
    except ValueError:
        print("  Invalid selection.")
        pause()
        return

    if idx == len(SERVICES) + 1:
        targets = [svc for svc, _ in SERVICES]
    elif 1 <= idx <= len(SERVICES):
        targets = [SERVICES[idx - 1][0]]
    else:
        print("  Invalid selection.")
        pause()
        return

    for svc in targets:
        print(f"  Restarting {svc}...")
        r = _run(["systemctl", "restart", svc], timeout=30, sudo=True)
        if r.returncode == 0:
            print(f"    {GREEN}OK{RESET}")
        else:
            print(f"    {RED}Failed: {r.stderr.strip()}{RESET}")

    pause()


def action_update() -> None:
    """6) Update BrainrotFilter"""
    clear_screen()
    print(f"\n{BOLD}  === Update BrainrotFilter ==={RESET}\n")
    print("  This will run: apt update && apt upgrade brainrotfilter -y\n")

    if not confirm("Proceed with update?"):
        print("  Cancelled.")
        pause()
        return

    print("\n  Updating package lists...")
    rc = _run_interactive(_sudo(["apt", "update"]), timeout=120)
    if rc != 0:
        print(f"\n  {RED}apt update failed (exit {rc}).{RESET}")
        pause()
        return

    print("\n  Upgrading brainrotfilter...")
    rc = _run_interactive(_sudo(["apt", "upgrade", "brainrotfilter", "-y"]), timeout=300)
    if rc == 0:
        print(f"\n  {GREEN}Update complete.{RESET}")
    else:
        print(f"\n  {RED}Upgrade failed (exit {rc}).{RESET}")

    pause()


def action_toggle_ssh() -> None:
    """7) Enable/Disable SSH"""
    clear_screen()
    print(f"\n{BOLD}  === SSH Service ==={RESET}\n")

    state = get_ssh_state()
    print(f"  Current state: {_state_color(state)}\n")

    if state == "active":
        fp = get_ssh_fingerprints()
        print(f"  Host fingerprint: {fp}\n")
        if confirm("Disable SSH?"):
            _run(["systemctl", "disable", "--now", "ssh.service"], sudo=True)
            # Mask again so the ssh.socket unit can't activate it either —
            # matches the default appliance state from the harden hook.
            r = _run(["systemctl", "mask", "ssh.service", "ssh.socket"], sudo=True)
            if r.returncode == 0:
                print(f"  {GREEN}SSH disabled.{RESET}")
            else:
                print(f"  {RED}Failed: {r.stderr.strip()}{RESET}")
    else:
        if confirm("Enable SSH?"):
            # Harden hook masks ssh.service + ssh.socket at build time;
            # enable --now on a masked unit fails. Unmask first.
            _run(["systemctl", "unmask", "ssh.service", "ssh.socket"], sudo=True)
            r = _run(["systemctl", "enable", "--now", "ssh.service"], sudo=True)
            if r.returncode == 0:
                print(f"  {GREEN}SSH enabled.{RESET}")
                fp = get_ssh_fingerprints()
                print(f"  Host fingerprint: {fp}")
            else:
                print(f"  {RED}Failed: {r.stderr.strip()}{RESET}")

    pause()


def action_shell() -> None:
    """8) Shell"""
    clear_screen()
    print(f"\n{YELLOW}  Dropping to shell. Type 'exit' to return to this menu.{RESET}\n")
    _run_interactive(["/bin/bash", "--login"], timeout=86400)


def action_reboot_shutdown() -> None:
    """9) Reboot / Shutdown"""
    clear_screen()
    print(f"\n{BOLD}  === Reboot / Shutdown ==={RESET}\n")
    print("    1) Reboot")
    print("    2) Shutdown")
    print("    0) Cancel")
    print()

    try:
        choice = input("  Select: ").strip()
    except EOFError:
        return

    if choice == "1":
        if confirm("Reboot now?"):
            print("  Rebooting...")
            _run(["systemctl", "reboot"], sudo=True)
    elif choice == "2":
        if confirm("Shut down now?"):
            print("  Shutting down...")
            _run(["systemctl", "poweroff"], sudo=True)
    else:
        return


# ---------------------------------------------------------------------------
# Menu definition
# ---------------------------------------------------------------------------

MENU_ITEMS = [
    ("0", "System Status", action_system_status),
    ("1", "Assign Interfaces", action_assign_interfaces),
    ("2", "Set Management IP", action_set_management_ip),
    ("3", "Reset Admin Password", action_reset_admin_password),
    ("4", "Factory Reset", action_factory_reset),
    ("5", "Restart Services", action_restart_services),
    ("6", "Update BrainrotFilter", action_update),
    ("7", "Enable/Disable SSH", action_toggle_ssh),
    ("8", "Shell", action_shell),
    ("9", "Reboot / Shutdown", action_reboot_shutdown),
]


def print_menu() -> None:
    print()
    for key, label, _ in MENU_ITEMS:
        print(f"    {BOLD}{key}){RESET} {label}")
    print()


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


def _sigint_handler(sig, frame):
    print(f"\n\n  {YELLOW}Use option 9 to reboot or shut down.{RESET}\n")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> None:
    signal.signal(signal.SIGINT, _sigint_handler)

    # Ensure we're not buffering
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    while True:
        try:
            clear_screen()
            print_header()
            print_menu()

            try:
                choice = input("  Enter an option: ").strip()
            except EOFError:
                continue

            # Dispatch
            dispatched = False
            for key, _, action in MENU_ITEMS:
                if choice == key:
                    action()
                    dispatched = True
                    break

            if not dispatched and choice:
                print(f"\n  {RED}Invalid option: {choice}{RESET}")
                pause()

        except KeyboardInterrupt:
            # Handled by signal handler, but just in case
            continue
        except Exception as exc:
            print(f"\n  {RED}Unexpected error: {exc}{RESET}")
            pause()


if __name__ == "__main__":
    main()
