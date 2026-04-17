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
    """Format the Web UI URL; returns a placeholder when IP is unset."""
    host = mgmt_ip.split("/")[0].strip() if mgmt_ip else ""
    if not host or host == "N/A":
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


def action_assign_interfaces() -> None:
    """1) Assign Interfaces"""
    clear_screen()
    mgmt_ip = get_management_ip()
    print(f"\n{BOLD}  === Assign Interfaces ==={RESET}\n")
    print(f"  Interface assignment is managed through the web UI.")
    print(f"  Open a browser and navigate to:\n")
    print(f"    {BOLD}{_format_web_url(mgmt_ip)}{RESET}\n")
    print(f"  The setup wizard will guide you through WAN/LAN NIC selection")
    print(f"  and bridge configuration.")
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

    # Determine interface
    iface = BRIDGE_IFACE
    r = _run(["ip", "link", "show", "dev", BRIDGE_IFACE])
    if r.returncode != 0:
        # No bridge — use default route interface
        r2 = _run(["ip", "-4", "route", "show", "default"])
        if r2.returncode == 0 and "dev" in r2.stdout:
            parts = r2.stdout.strip().split()
            iface = parts[parts.index("dev") + 1]

    # Build netplan YAML
    addresses_block = f"      addresses:\n        - {ip_cidr}"
    gw_block = f"\n      routes:\n        - to: default\n          via: {gateway}" if gateway else ""
    dns_block = f"\n      nameservers:\n        addresses:\n          - {dns}"

    netplan_yaml = textwrap.dedent(f"""\
        # BrainrotFilter management network — written by console TUI
        network:
          version: 2
          renderer: networkd
          ethernets:
            {iface}:
        {addresses_block}{gw_block}{dns_block}
    """)

    print(f"\n  Will write the following netplan config for {iface}:\n")
    for line in netplan_yaml.splitlines():
        print(f"    {line}")
    print()

    if not confirm("Apply this configuration?"):
        print("  Cancelled.")
        pause()
        return

    netplan_path = "/etc/netplan/90-brainrotfilter-mgmt.yaml"
    # Write via `sudo -n tee` — appliance user can't write /etc/netplan/ directly.
    proc = subprocess.run(
        _sudo(["tee", netplan_path]),
        input=netplan_yaml,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"  {RED}Failed to write netplan config: {proc.stderr.strip()}{RESET}")
        pause()
        return

    # Netplan files must be mode 600 or looser only for root-readable reasons;
    # set 600 explicitly to match Ubuntu 24.04's tightened permissions.
    _run(["chmod", "600", netplan_path], sudo=True)

    # Apply
    print("  Applying netplan...")
    r = _run(["netplan", "apply"], timeout=30, sudo=True)
    if r.returncode == 0:
        print(f"  {GREEN}Network configuration applied.{RESET}")
    else:
        print(f"  {RED}netplan apply failed: {r.stderr.strip()}{RESET}")

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
            r = _run(["systemctl", "disable", "--now", "ssh"], sudo=True)
            if r.returncode == 0:
                print(f"  {GREEN}SSH disabled.{RESET}")
            else:
                print(f"  {RED}Failed: {r.stderr.strip()}{RESET}")
    else:
        if confirm("Enable SSH?"):
            r = _run(["systemctl", "enable", "--now", "ssh"], sudo=True)
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
