"""
linux_configurator.py
=====================
Local Linux system configurator for BrainrotFilter.

Configures the local Linux system for transparent proxy filtering.
  - Generate self-signed CA using openssl
  - Configure Squid with SSL bump (peek/stare/bump for YouTube, splice rest)
  - Setup iptables REDIRECT rules for transparent proxy
  - Block QUIC/HTTP3 via iptables DROP UDP 443/80
  - Enable IP forwarding
  - Write /etc/brainrotfilter/brainrotfilter.env
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("brainrotfilter.linux_configurator")

# Paths
CONFIG_DIR = Path("/etc/brainrotfilter")
DATA_DIR = Path("/var/lib/brainrotfilter")
LOG_DIR = Path("/var/log/brainrotfilter")
CA_DIR = CONFIG_DIR / "ssl"
SQUID_CONF_DIR = Path("/etc/squid")
SQUID_SSL_DB = Path("/var/lib/squid/ssl_db")
SCRIPTS_DIR = Path("/usr/lib/brainrotfilter/scripts")

# YouTube domains for Squid ACLs
YOUTUBE_DOMAINS = [
    ".youtube.com",
    ".googlevideo.com",
    ".ytimg.com",
    ".ggpht.com",
]


@dataclass
class LinuxConfig:
    """Configuration parameters for the Linux setup."""
    network_interface: str = "eth0"
    squid_http_port: int = 3128
    squid_https_port: int = 3129
    service_port: int = 8199
    ca_name: str = "BrainrotFilter CA"
    ca_days: int = 3650
    ca_key_size: int = 4096
    ssl_pinning_methods: List[str] = field(default_factory=list)
    block_quic: bool = True
    enable_ip_forward: bool = True


class LinuxConfigurator:
    """Configures the local Linux system for BrainrotFilter."""

    def __init__(self, config: Optional[LinuxConfig] = None):
        self.config = config or LinuxConfig()

    # -- Utility -----------------------------------------------------------

    @staticmethod
    def _run(cmd: List[str], timeout: int = 30, check: bool = False) -> subprocess.CompletedProcess:
        """Run a command and return the result."""
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=check,
            )
        except FileNotFoundError:
            return subprocess.CompletedProcess(cmd, -1, "", f"Command not found: {cmd[0]}")
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, -1, "", "Command timed out")

    # -- Detection ---------------------------------------------------------

    def detect_state(self) -> Dict[str, Any]:
        """Detect current system configuration state."""
        state: Dict[str, Any] = {
            "squid_installed": False,
            "squid_running": False,
            "ca_exists": False,
            "iptables_rules": False,
            "ip_forward_enabled": False,
            "helpers_installed": False,
            "service_active": False,
            "network_interfaces": [],
        }

        # Check Squid
        r = self._run(["which", "squid"])
        state["squid_installed"] = r.returncode == 0

        r = self._run(["systemctl", "is-active", "squid"])
        state["squid_running"] = r.stdout.strip() == "active"

        # Check CA
        ca_cert = CA_DIR / "brainrotfilter-ca.pem"
        state["ca_exists"] = ca_cert.exists()

        # Check iptables rules
        r = self._run(["iptables", "-t", "nat", "-L", "PREROUTING", "-n"])
        if r.returncode == 0:
            state["iptables_rules"] = "3128" in r.stdout or "3129" in r.stdout

        # Check IP forwarding
        try:
            val = Path("/proc/sys/net/ipv4/ip_forward").read_text().strip()
            state["ip_forward_enabled"] = val == "1"
        except Exception:
            pass

        # Check helpers
        state["helpers_installed"] = (SCRIPTS_DIR / "squid_redirector.sh").exists()

        # Check service
        r = self._run(["systemctl", "is-active", "brainrotfilter"])
        state["service_active"] = r.stdout.strip() == "active"

        # List network interfaces -- include MAC, operstate, carrier, and
        # link speed so the wizard can help the user identify WAN vs LAN.
        try:
            interfaces = []
            for iface in Path("/sys/class/net").iterdir():
                name = iface.name
                if name == "lo":
                    continue
                info: Dict[str, Any] = {
                    "name": name,
                    "mac": "",
                    "state": "unknown",
                    "carrier": None,
                    "speed": None,
                    "is_bridge": (iface / "bridge").exists(),
                    "is_virtual": name.startswith(("br", "veth", "docker", "tun", "tap", "wg")),
                }
                try:
                    info["mac"] = (iface / "address").read_text().strip()
                except Exception:
                    pass
                try:
                    info["state"] = (iface / "operstate").read_text().strip()
                except Exception:
                    pass
                try:
                    info["carrier"] = (iface / "carrier").read_text().strip() == "1"
                except Exception:
                    info["carrier"] = None
                try:
                    raw = (iface / "speed").read_text().strip()
                    info["speed"] = int(raw) if raw.lstrip("-").isdigit() else None
                except Exception:
                    info["speed"] = None
                interfaces.append(info)
            state["network_interfaces"] = interfaces
        except Exception:
            pass

        return state

    # -- CA Certificate ----------------------------------------------------

    def create_ca(self, ca_name: Optional[str] = None) -> Dict[str, Any]:
        """Create a self-signed CA certificate using openssl."""
        ca_name = ca_name or self.config.ca_name
        CA_DIR.mkdir(parents=True, exist_ok=True)

        ca_key = CA_DIR / "brainrotfilter-ca.key"
        ca_cert = CA_DIR / "brainrotfilter-ca.pem"

        if ca_cert.exists():
            logger.info("CA certificate already exists at %s", ca_cert)
            return {
                "success": True,
                "ca_cert_path": str(ca_cert),
                "already_existed": True,
            }

        # Generate CA private key
        r = self._run([
            "openssl", "genrsa",
            "-out", str(ca_key),
            str(self.config.ca_key_size),
        ])
        if r.returncode != 0:
            return {"success": False, "error": f"Failed to generate CA key: {r.stderr}"}

        # Generate self-signed CA certificate
        r = self._run([
            "openssl", "req",
            "-new", "-x509",
            "-days", str(self.config.ca_days),
            "-key", str(ca_key),
            "-out", str(ca_cert),
            "-subj", f"/C=US/ST=Network/L=BrainrotFilter/O=BrainrotFilter/CN={ca_name}",
        ])
        if r.returncode != 0:
            return {"success": False, "error": f"Failed to generate CA cert: {r.stderr}"}

        # Set permissions
        os.chmod(str(ca_key), 0o600)
        os.chmod(str(ca_cert), 0o644)

        logger.info("CA certificate created: %s", ca_cert)
        return {
            "success": True,
            "ca_cert_path": str(ca_cert),
            "ca_key_path": str(ca_key),
            "already_existed": False,
        }

    def get_ca_cert_pem(self) -> Optional[str]:
        """Return the CA certificate PEM text, or None if not found."""
        ca_cert = CA_DIR / "brainrotfilter-ca.pem"
        if ca_cert.exists():
            return ca_cert.read_text()
        return None

    # -- Squid Configuration -----------------------------------------------

    def configure_squid(self) -> Dict[str, Any]:
        """Write BrainrotFilter Squid configuration snippet."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        ca_cert = CA_DIR / "brainrotfilter-ca.pem"
        ca_key = CA_DIR / "brainrotfilter-ca.key"

        if not ca_cert.exists():
            return {"success": False, "error": "CA certificate not found. Create CA first."}

        # Initialize SSL certificate database for Squid
        if not SQUID_SSL_DB.exists():
            r = self._run([
                "/usr/lib/squid/security_file_certgen",
                "-c", "-s", str(SQUID_SSL_DB),
                "-M", "16MB",
            ])
            if r.returncode != 0:
                # Try alternative path
                r = self._run([
                    "/usr/lib64/squid/security_file_certgen",
                    "-c", "-s", str(SQUID_SSL_DB),
                    "-M", "16MB",
                ])
            if r.returncode == 0:
                # Set ownership
                self._run(["chown", "-R", "proxy:proxy", str(SQUID_SSL_DB)])

        # Write Squid configuration snippet
        squid_conf = f"""\
# BrainrotFilter Squid Configuration
# Auto-generated by BrainrotFilter setup wizard
# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}

# -- Ports --
http_port {self.config.squid_http_port} intercept
https_port {self.config.squid_https_port} intercept ssl-bump \\
    tls-cert={ca_cert} \\
    tls-key={ca_key} \\
    generate-host-certificates=on \\
    dynamic_cert_mem_cache_size=16MB

sslcrtd_program /usr/lib/squid/security_file_certgen -s {SQUID_SSL_DB} -M 16MB
sslcrtd_children 5 startup=1 idle=1

# -- YouTube ACLs --
acl youtube_domains dstdomain {' '.join(YOUTUBE_DOMAINS)}
acl youtube_sites ssl::server_name {' '.join(YOUTUBE_DOMAINS)}

# -- SSL Bump Rules --
acl step1 at_step SslBump1
acl step2 at_step SslBump2
acl step3 at_step SslBump3

ssl_bump peek step1 all
ssl_bump stare step2 youtube_sites
ssl_bump bump step3 youtube_sites
ssl_bump splice all

# -- URL Rewriter --
# Narrow to active-playback URLs only, via a url_regex pre-filter. Without
# this the redirector is invoked for every thumbnail / feed API / telemetry
# request, which swamps the helper pool and stalls the home page.
acl brainrot_rewrite_url url_regex -i youtube\\.com/watch youtube\\.com/shorts/ youtube\\.com/embed/ youtu\\.be/ youtube\\.com/api/stats/watchtime youtube\\.com/api/stats/qoe youtube\\.com/api/stats/playback youtube\\.com/youtubei/v1/player youtube\\.com/youtubei/v1/next ytimg\\.com/sb/
url_rewrite_program {SCRIPTS_DIR}/squid_redirector.sh
url_rewrite_children 20 startup=3 idle=1 concurrency=1
url_rewrite_access allow brainrot_rewrite_url
url_rewrite_access deny all
url_rewrite_bypass on

# -- Disable cache for YouTube --
acl youtube_nocache dstdomain {' '.join(YOUTUBE_DOMAINS)}
cache deny youtube_nocache
no_cache deny youtube_nocache

# -- Access rules --
# Allow traffic from any RFC 1918 / private range.
# Note: main squid.conf defines 'localnet' already; we add a broader
# catch-all so this snippet works even if inserted before deny all.
http_access allow localnet
http_access allow localhost

# -- Misc --
host_verify_strict off

# -- ICAP integration --
# Intercept youtubei player/next POST bodies for brainrot classification.
acl youtube_youtubei_url url_regex -i youtube\\.com/youtubei/v1/(player|next)
icap_enable on
icap_service brainrot_req reqmod_precache icap://127.0.0.1:1344/brainrot bypass=on
adaptation_service_set brainrot_yti brainrot_req
adaptation_access brainrot_yti allow youtube_youtubei_url
adaptation_access brainrot_yti deny all
"""

        conf_path = CONFIG_DIR / "squid_brainrot.conf"
        conf_path.write_text(squid_conf)

        # Write the conf.d snippet — this goes in /etc/squid/conf.d/ which is
        # included by the stock squid.conf BEFORE http_access allow localnet.
        # That ordering lets us deny blocked videos before the allow-all fires.
        #
        # We use two separate external_acl_type instances so hard blocks and
        # soft blocks can redirect to different pages.
        import socket as _socket
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as _s:
                _s.connect(("8.8.8.8", 80))
                _redirect_ip = _s.getsockname()[0]
        except Exception:
            _redirect_ip = "127.0.0.1"
        _port = self.config.service_port
        confd_content = (
            "# BrainrotFilter Squid ACL -- managed by BrainrotFilter wizard\n"
            f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            "#\n"
            "# Loaded via 'include /etc/squid/conf.d/*.conf' BEFORE http_access allow localnet.\n"
            "# Deny rules here fire before the catch-all allow, making blocking effective.\n"
            "\n"
            "# Fast pre-filter: only URLs that could indicate active video playback.\n"
            "# Squid's url_regex is evaluated cheaply and short-circuits before any\n"
            "# expensive external helper is invoked. Thumbnails, qoe telemetry,\n"
            "# youtubei metadata, and home-feed API calls skip the helpers entirely.\n"
            "acl youtube_playback_url url_regex -i youtube\\.com/watch youtube\\.com/shorts/ youtube\\.com/embed/ youtu\\.be/ youtube\\.com/api/stats/watchtime youtube\\.com/api/stats/qoe youtube\\.com/api/stats/playback youtube\\.com/youtubei/v1/player youtube\\.com/youtubei/v1/next ytimg\\.com/sb/\n"
            "\n"
            "# Hard-block tier (status=block) -- redirect to block page\n"
            f"external_acl_type brainrot_block_check children-max=20 children-startup=3 ttl=60 negative_ttl=30 %URI %SRC {SCRIPTS_DIR}/squid_acl_helper.sh block\n"
            "acl brainrot_hard_blocked external brainrot_block_check\n"
            f"deny_info http://{_redirect_ip}:{_port}/blocked brainrot_hard_blocked\n"
            "http_access deny youtube_playback_url brainrot_hard_blocked\n"
            "\n"
            "# Soft-block tier (status=soft_block) -- redirect to warning page\n"
            f"external_acl_type brainrot_soft_check children-max=20 children-startup=3 ttl=60 negative_ttl=30 %URI %SRC {SCRIPTS_DIR}/squid_acl_helper.sh soft_block\n"
            "acl brainrot_soft_blocked external brainrot_soft_check\n"
            f"deny_info http://{_redirect_ip}:{_port}/warning brainrot_soft_blocked\n"
            "http_access deny youtube_playback_url brainrot_soft_blocked\n"
            "\n"
            "# Pre-emptive CDN block — deny googlevideo.com while the client's\n"
            "# currently-watching video is still being analysed.  Prevents any\n"
            "# content from being pre-buffered before the verdict is known.\n"
            "# Replaces an earlier delay_pool approach that throttled all\n"
            "# googlevideo.com traffic to 256Kbps — allowed videos now play\n"
            "# at full native quality/speed.\n"
            f"external_acl_type brainrot_cdn_pending children-max=20 children-startup=5 concurrency=0 ttl=3 negative_ttl=3 %SRC {SCRIPTS_DIR}/squid_cdn_block_helper.sh\n"
            "acl brainrot_client_pending external brainrot_cdn_pending\n"
            "acl youtube_cdn_domains dstdomain .googlevideo.com\n"
            "http_access deny brainrot_client_pending youtube_cdn_domains\n"
            "\n"
            "# ICAP body-inspection shim — allow the brainrotfilter shim URL\n"
            "# so the ICAP client can POST captured request bodies back to the\n"
            "# local service for classification.\n"
            f"acl brainrot_shim_url url_regex -i http://{_redirect_ip}:{_port}/icap_shim\n"
            "http_access allow brainrot_shim_url\n"
        )
        confd_request = DATA_DIR / "squid_confd_content"
        try:
            confd_request.write_text(confd_content)
        except Exception as exc:
            return {
                "success": False,
                "error": f"Could not write conf.d request file {confd_request}: {exc}",
            }

        # Add include to main squid.conf if not present.
        #
        # /etc/squid/squid.conf is owned by root. The brainrotfilter service
        # runs with NoNewPrivileges=true so sudo is not available. Instead we:
        #   1. Write the desired snippet path to a request file.
        #   2. Ask systemd to start brainrotfilter-squid-apply.service (a
        #      root oneshot unit) via `systemctl start`, which is permitted
        #      by the polkit rule installed in postinst.
        main_conf = SQUID_CONF_DIR / "squid.conf"
        include_line = f"include {conf_path}"

        needs_update = False
        if main_conf.exists():
            try:
                content = main_conf.read_text()
                needs_update = str(conf_path) not in content
            except PermissionError:
                # Can't read it either — assume we need to update
                needs_update = True

        if needs_update:
            request_file = DATA_DIR / "squid_include_path"
            try:
                request_file.write_text(str(conf_path) + "\n")
            except Exception as exc:
                return {
                    "success": False,
                    "error": f"Could not write request file {request_file}: {exc}",
                }

            # Trigger the privileged helper via systemd
            r = self._run(
                ["systemctl", "start", "brainrotfilter-squid-apply.service"],
                timeout=15,
            )
            if r.returncode != 0:
                return {
                    "success": False,
                    "error": (
                        f"Failed to start brainrotfilter-squid-apply.service "
                        f"(returncode={r.returncode}): {r.stderr[:300]}. "
                        "Ensure the polkit rule is installed: "
                        "sudo dpkg-reconfigure brainrotfilter"
                    ),
                }
            logger.info("Squid include directive written via privileged helper.")

        # Test configuration
        r = self._run(["squid", "-k", "parse"], timeout=15)
        if r.returncode != 0 and "FATAL" in (r.stderr or ""):
            return {
                "success": False,
                "error": f"Squid config validation failed: {r.stderr[:400]}",
            }

        return {"success": True, "config_path": str(conf_path)}

    # -- Transparent L2 Bridge --------------------------------------------

    def configure_bridge(
        self,
        wan_nic: str,
        lan_nic: str,
        mgmt_ip: Optional[str] = None,
        mgmt_mask: int = 24,
        gateway: str = "",
        dns: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Configure a transparent L2 bridge across two NICs.

        Writes a netplan config creating ``br0`` with ``wan_nic`` and
        ``lan_nic`` as members (STP disabled -- we don't want the box to
        participate in spanning-tree decisions of the upstream network).
        The management IP lives on ``br0`` itself, either static or DHCP.

        Also ensures ``br_netfilter`` is loaded and the corresponding sysctl
        knobs are set so that iptables sees bridged traffic -- without this
        the PREROUTING REDIRECT rules would never match bridged packets.

        Parameters
        ----------
        wan_nic, lan_nic : str
            Interface names for the WAN-facing and LAN-facing NICs. Both
            become members of br0 (L2). The distinction is only meaningful
            as a label -- both NICs are treated identically at L2.
        mgmt_ip : Optional[str]
            Static management IP to assign to br0 (e.g. "192.168.1.10").
            When ``None`` the bridge falls back to DHCP.
        mgmt_mask : int
            CIDR prefix length for the management IP (default /24).
        gateway : str
            Default gateway for the management interface (ignored for DHCP).
        dns : Optional[List[str]]
            DNS servers to configure on br0 (ignored for DHCP).
        """
        dns = dns or []
        netplan_dir = Path("/etc/netplan")
        bridge_yaml = netplan_dir / "99-brainrotfilter-bridge.yaml"
        cloud_init_yaml = netplan_dir / "50-cloud-init.yaml"

        # Build the br0 config block.
        if mgmt_ip:
            addr_line = f"      addresses: [{mgmt_ip}/{mgmt_mask}]\n"
            if gateway:
                # routes: is the modern netplan equivalent of gateway4.
                addr_line += (
                    "      routes:\n"
                    f"        - to: default\n"
                    f"          via: {gateway}\n"
                )
            if dns:
                ns_list = ", ".join(dns)
                addr_line += (
                    "      nameservers:\n"
                    f"        addresses: [{ns_list}]\n"
                )
            dhcp_line = "      dhcp4: false\n"
        else:
            addr_line = ""
            dhcp_line = "      dhcp4: true\n"

        netplan_content = (
            "# BrainrotFilter transparent-bridge netplan config\n"
            "# Auto-generated by the wizard. Edit at your own risk.\n"
            "network:\n"
            "  version: 2\n"
            "  renderer: networkd\n"
            "  ethernets:\n"
            f"    {wan_nic}:\n"
            "      dhcp4: false\n"
            "      dhcp6: false\n"
            "      optional: true\n"
            f"    {lan_nic}:\n"
            "      dhcp4: false\n"
            "      dhcp6: false\n"
            "      optional: true\n"
            "  bridges:\n"
            "    br0:\n"
            f"      interfaces: [{wan_nic}, {lan_nic}]\n"
            "      parameters:\n"
            "        stp: false\n"
            "        forward-delay: 0\n"
            f"{dhcp_line}"
            f"{addr_line}"
        )

        try:
            netplan_dir.mkdir(parents=True, exist_ok=True)
            bridge_yaml.write_text(netplan_content)
            os.chmod(str(bridge_yaml), 0o600)
        except Exception as exc:
            return {"success": False, "error": f"Could not write {bridge_yaml}: {exc}"}

        # Comment out any `dhcp4: true` on the individual NICs in the
        # cloud-init netplan so we don't fight for the interfaces.
        cloud_init_patched = False
        if cloud_init_yaml.exists():
            try:
                original = cloud_init_yaml.read_text()
                patched_lines = []
                # We only rewrite lines that live under a matching NIC stanza.
                current_iface = None
                for line in original.splitlines():
                    stripped = line.strip()
                    # Detect top-level interface entries (two-space indent under
                    # `ethernets:` is the typical cloud-init layout).
                    if stripped.endswith(":") and not stripped.startswith("#"):
                        name = stripped.rstrip(":").strip()
                        if name in (wan_nic, lan_nic):
                            current_iface = name
                        elif line[:2] == "  " and line[2:3] != " ":
                            # New top-level key -- reset.
                            current_iface = None
                    if current_iface and "dhcp4:" in line and "true" in line.lower():
                        # Prefix comment preserving indent.
                        indent = line[: len(line) - len(line.lstrip())]
                        patched_lines.append(
                            f"{indent}# dhcp4: true  # disabled by BrainrotFilter bridge setup"
                        )
                        cloud_init_patched = True
                        continue
                    patched_lines.append(line)
                if cloud_init_patched:
                    # Back up once, then rewrite.
                    backup = cloud_init_yaml.with_suffix(
                        cloud_init_yaml.suffix + ".brainrotfilter.bak"
                    )
                    if not backup.exists():
                        backup.write_text(original)
                    cloud_init_yaml.write_text("\n".join(patched_lines) + "\n")
            except Exception as exc:
                logger.warning("Could not patch %s: %s", cloud_init_yaml, exc)

        # Ensure br_netfilter is loaded at boot.
        try:
            modules_conf = Path("/etc/modules-load.d/brainrotfilter.conf")
            modules_conf.parent.mkdir(parents=True, exist_ok=True)
            modules_conf.write_text("br_netfilter\n")
        except Exception as exc:
            return {"success": False, "error": f"Could not write modules-load conf: {exc}"}

        # Load the module right now so sysctl knobs exist.
        self._run(["modprobe", "br_netfilter"])

        # Bridge-netfilter sysctls -- iptables must see bridged packets,
        # arptables must not (arp on a transparent bridge should stay L2).
        try:
            sysctl_bridge = Path("/etc/sysctl.d/99-brainrotfilter-bridge.conf")
            sysctl_bridge.parent.mkdir(parents=True, exist_ok=True)
            sysctl_bridge.write_text(
                "net.bridge.bridge-nf-call-iptables=1\n"
                "net.bridge.bridge-nf-call-ip6tables=1\n"
                "net.bridge.bridge-nf-call-arptables=0\n"
            )
            # Apply immediately, ignoring errors (module may not be up yet
            # on first run; reboot / `netplan apply` will pick it up).
            for key, val in (
                ("net.bridge.bridge-nf-call-iptables", "1"),
                ("net.bridge.bridge-nf-call-ip6tables", "1"),
                ("net.bridge.bridge-nf-call-arptables", "0"),
            ):
                self._run(["sysctl", "-w", f"{key}={val}"])
        except Exception as exc:
            return {"success": False, "error": f"Could not write sysctl conf: {exc}"}

        # Record the bridge interface on the config so subsequent
        # setup_iptables() calls default to br0.
        self.config.network_interface = "br0"

        applied = {
            "wan_nic": wan_nic,
            "lan_nic": lan_nic,
            "bridge": "br0",
            "mgmt_ip": mgmt_ip,
            "mgmt_mask": mgmt_mask,
            "gateway": gateway,
            "dns": dns,
            "dhcp": mgmt_ip is None,
            "netplan_file": str(bridge_yaml),
            "cloud_init_patched": cloud_init_patched,
        }

        return {"success": True, "config": applied}

    # -- iptables Rules ----------------------------------------------------

    def setup_iptables(self, interface: Optional[str] = None) -> Dict[str, Any]:
        """Set up iptables rules for transparent proxy and QUIC blocking.

        Parameters
        ----------
        interface : Optional[str]
            The interface to apply PREROUTING REDIRECT rules to. When running
            in transparent-bridge mode this should be ``br0`` so that the
            bridge's virtual interface is matched (bridged packets traverse
            nf_call_iptables and hit PREROUTING). When ``None`` the value
            from ``self.config.network_interface`` is used (single-NIC mode).
        """
        results = {"success": True, "rules_added": 0, "errors": []}
        iface = interface or self.config.network_interface
        # Detect bridge-mode so we can skip NAT MASQUERADE. Bridge mode is a
        # pure L2 transparent interception -- there is no routing decision
        # being made on this box, so masquerading would be wrong (and break
        # the return path). We ONLY install PREROUTING REDIRECT + FORWARD DROP
        # rules; no POSTROUTING MASQUERADE is ever added here.
        bridge_mode = iface.startswith("br")
        results["bridge_mode"] = bridge_mode
        results["interface"] = iface

        # Enable IP forwarding
        if self.config.enable_ip_forward:
            try:
                Path("/proc/sys/net/ipv4/ip_forward").write_text("1")
                # Make persistent
                sysctl_conf = Path("/etc/sysctl.d/99-brainrotfilter.conf")
                sysctl_conf.parent.mkdir(parents=True, exist_ok=True)
                sysctl_conf.write_text("net.ipv4.ip_forward = 1\n")
            except Exception as e:
                results["errors"].append(f"IP forwarding: {e}")

        # NAT REDIRECT rules for transparent proxy
        nat_rules = [
            # HTTP -> Squid HTTP intercept
            ["-t", "nat", "-A", "PREROUTING", "-i", iface, "-p", "tcp",
             "--dport", "80", "-j", "REDIRECT", "--to-port",
             str(self.config.squid_http_port)],
            # HTTPS -> Squid HTTPS intercept
            ["-t", "nat", "-A", "PREROUTING", "-i", iface, "-p", "tcp",
             "--dport", "443", "-j", "REDIRECT", "--to-port",
             str(self.config.squid_https_port)],
        ]

        for rule in nat_rules:
            # Check if rule already exists
            check_rule = [r if r != "-A" else "-C" for r in rule]
            r = self._run(["iptables"] + check_rule)
            if r.returncode != 0:  # Rule doesn't exist, add it
                r = self._run(["iptables"] + rule)
                if r.returncode == 0:
                    results["rules_added"] += 1
                else:
                    results["errors"].append(f"iptables: {r.stderr}")

        # Block QUIC/HTTP3 (UDP 443 and UDP 80)
        if self.config.block_quic:
            quic_rules = [
                ["-A", "FORWARD", "-i", iface, "-p", "udp",
                 "--dport", "443", "-j", "DROP",
                 "-m", "comment", "--comment", "BrainrotFilter: Block QUIC/HTTP3"],
                ["-A", "FORWARD", "-i", iface, "-p", "udp",
                 "--dport", "80", "-j", "DROP",
                 "-m", "comment", "--comment", "BrainrotFilter: Block QUIC/HTTP3"],
            ]
            for rule in quic_rules:
                check_rule = [r if r != "-A" else "-C" for r in rule]
                r = self._run(["iptables"] + check_rule)
                if r.returncode != 0:
                    r = self._run(["iptables"] + rule)
                    if r.returncode == 0:
                        results["rules_added"] += 1
                    else:
                        results["errors"].append(f"iptables QUIC: {r.stderr}")

        # Save iptables rules for persistence
        self._save_iptables()

        if results["errors"]:
            results["success"] = len(results["errors"]) < len(nat_rules)
        return results

    def remove_iptables(self) -> Dict[str, Any]:
        """Remove all BrainrotFilter iptables rules."""
        removed = 0

        # Remove NAT rules
        for port in ["80", "443"]:
            for squid_port in [str(self.config.squid_http_port), str(self.config.squid_https_port)]:
                r = self._run([
                    "iptables", "-t", "nat", "-D", "PREROUTING",
                    "-p", "tcp", "--dport", port,
                    "-j", "REDIRECT", "--to-port", squid_port,
                ])
                if r.returncode == 0:
                    removed += 1

        # Remove QUIC blocking rules (try to find and remove by comment)
        r = self._run(["iptables", "-L", "FORWARD", "--line-numbers", "-n"])
        if r.returncode == 0:
            # Parse line numbers for BrainrotFilter rules (in reverse to avoid index shift)
            lines = r.stdout.strip().split("\n")
            line_nums = []
            for line in lines:
                if "BrainrotFilter" in line:
                    parts = line.split()
                    if parts and parts[0].isdigit():
                        line_nums.append(int(parts[0]))
            for num in sorted(line_nums, reverse=True):
                self._run(["iptables", "-D", "FORWARD", str(num)])
                removed += 1

        # Remove sysctl config
        sysctl_conf = Path("/etc/sysctl.d/99-brainrotfilter.conf")
        if sysctl_conf.exists():
            sysctl_conf.unlink()

        self._save_iptables()
        return {"success": True, "removed": removed}

    @staticmethod
    def _save_iptables() -> None:
        """Save iptables rules for persistence across reboots."""
        try:
            subprocess.run(
                ["sh", "-c", "iptables-save > /etc/iptables/rules.v4"],
                capture_output=True, timeout=10, check=False,
            )
        except Exception:
            pass

    # -- Shell Helper Scripts ----------------------------------------------

    def install_helpers(self, service_url: str = "http://127.0.0.1:8199") -> Dict[str, Any]:
        """Install shell helper scripts to SCRIPTS_DIR."""
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

        # Find scripts — check installed locations in priority order:
        #   1. SCRIPTS_DIR itself (dpkg already placed them there)
        #   2. Alongside the source tree (dev/git checkout)
        #   3. Legacy /usr/share location (not used by current packaging)
        pkg_scripts = SCRIPTS_DIR
        if not pkg_scripts.exists() or not any(pkg_scripts.iterdir()):
            pkg_scripts = Path(__file__).parent.parent.parent / "scripts"
        if not pkg_scripts.exists():
            pkg_scripts = Path("/usr/share/brainrotfilter/scripts")

        installed = []
        for script_name in ["squid_redirector.sh", "squid_acl_helper.sh",
                            "squid_cdn_block_helper.sh", "state_killer.sh"]:
            src = pkg_scripts / script_name
            dst = SCRIPTS_DIR / script_name
            if src.exists():
                shutil.copy2(str(src), str(dst))
                os.chmod(str(dst), 0o755)
                installed.append(script_name)
            else:
                logger.warning("Helper script not found: %s", src)

        # Detect the gateway IP dynamically (same socket trick as configure_squid)
        import socket as _socket
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as _s:
                _s.connect(("8.8.8.8", 80))
                _gateway_ip = _s.getsockname()[0]
        except Exception:
            _gateway_ip = "127.0.0.1"

        # Write environment config
        env_path = CONFIG_DIR / "brainrotfilter.env"
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            f"BRAINROT_API={service_url}\n"
            f"GATEWAY_IP={_gateway_ip}\n"
        )

        return {"success": True, "installed": installed}

    # -- SSL Pinning Bypass ------------------------------------------------

    def configure_ssl_pinning(self, methods: List[str]) -> Dict[str, Any]:
        """Configure SSL pinning bypass methods."""
        results: Dict[str, Any] = {}

        if "dns_block" in methods:
            # Add DNS entries to /etc/hosts to block YouTube app API endpoints
            hosts_entries = [
                "# BrainrotFilter: SSL pinning bypass - block YouTube app endpoints",
                "0.0.0.0 youtubei.googleapis.com",
                "0.0.0.0 play.googleapis.com",
            ]
            hosts_path = Path("/etc/hosts")
            content = hosts_path.read_text() if hosts_path.exists() else ""
            if "BrainrotFilter" not in content:
                with open(hosts_path, "a") as f:
                    f.write("\n" + "\n".join(hosts_entries) + "\n")
            results["dns_block"] = {"success": True}

        if "block_app" in methods:
            # Block YouTube app API via iptables
            r = self._run([
                "iptables", "-A", "FORWARD", "-p", "tcp",
                "-d", "youtubei.googleapis.com", "--dport", "443",
                "-j", "DROP",
                "-m", "comment", "--comment",
                "BrainrotFilter: Block YouTube App",
            ])
            results["block_app"] = {"success": r.returncode == 0}

        if "mdm_cert" in methods:
            results["mdm_cert"] = {
                "success": True,
                "note": "Export CA from /etc/brainrotfilter/ssl/brainrotfilter-ca.pem "
                        "for MDM deployment.",
            }

        if "proxy_redirect" in methods:
            results["proxy_redirect"] = {
                "success": True,
                "note": "Transparent proxy redirect is handled by iptables rules.",
            }

        return {"success": True, "results": results}

    # -- Service Management ------------------------------------------------

    def restart_squid(self) -> Dict[str, Any]:
        """Restart the Squid service."""
        r = self._run(["systemctl", "restart", "squid"], timeout=30)
        return {
            "success": r.returncode == 0,
            "error": r.stderr if r.returncode != 0 else None,
        }

    def verify_setup(self) -> Dict[str, Any]:
        """Verify the setup is working."""
        checks: Dict[str, Any] = {}

        # Squid running
        r = self._run(["systemctl", "is-active", "squid"])
        checks["squid_running"] = r.stdout.strip() == "active"

        # Helpers installed
        checks["helpers_installed"] = (SCRIPTS_DIR / "squid_redirector.sh").exists()

        # iptables rules
        r = self._run(["iptables", "-t", "nat", "-L", "PREROUTING", "-n"])
        checks["iptables_configured"] = (
            str(self.config.squid_http_port) in r.stdout
            if r.returncode == 0 else False
        )

        # CA cert exists
        checks["ca_exists"] = (CA_DIR / "brainrotfilter-ca.pem").exists()

        # Service running
        r = self._run(["systemctl", "is-active", "brainrotfilter"])
        checks["service_running"] = r.stdout.strip() == "active"

        return checks

    # -- Cleanup -----------------------------------------------------------

    def uninstall(self) -> Dict[str, Any]:
        """Remove all BrainrotFilter configuration from the system."""
        results = {"steps": []}

        # Remove iptables rules
        try:
            r = self.remove_iptables()
            results["steps"].append({"step": "iptables", "success": True, "removed": r.get("removed", 0)})
        except Exception as e:
            results["steps"].append({"step": "iptables", "success": False, "error": str(e)})

        # Remove Squid config snippet
        conf_path = CONFIG_DIR / "squid_brainrot.conf"
        if conf_path.exists():
            conf_path.unlink()
            results["steps"].append({"step": "squid_config", "success": True})

        # Remove include from squid.conf
        main_conf = SQUID_CONF_DIR / "squid.conf"
        if main_conf.exists():
            content = main_conf.read_text()
            if "brainrotfilter" in content.lower():
                lines = [
                    l for l in content.split("\n")
                    if "brainrotfilter" not in l.lower() and "BrainrotFilter" not in l
                ]
                main_conf.write_text("\n".join(lines))
                results["steps"].append({"step": "squid_conf_cleanup", "success": True})

        # Remove CA
        if CA_DIR.exists():
            shutil.rmtree(str(CA_DIR), ignore_errors=True)
            results["steps"].append({"step": "ca_removal", "success": True})

        # Remove helper scripts
        if SCRIPTS_DIR.exists():
            shutil.rmtree(str(SCRIPTS_DIR), ignore_errors=True)
            results["steps"].append({"step": "helpers_removal", "success": True})

        # Remove DNS entries
        hosts_path = Path("/etc/hosts")
        if hosts_path.exists():
            content = hosts_path.read_text()
            if "BrainrotFilter" in content:
                lines = [
                    l for l in content.split("\n")
                    if "BrainrotFilter" not in l
                    and "youtubei.googleapis.com" not in l
                    and "play.googleapis.com" not in l
                ]
                hosts_path.write_text("\n".join(lines))
                results["steps"].append({"step": "dns_cleanup", "success": True})

        # Restart Squid
        self._run(["systemctl", "restart", "squid"])

        results["success"] = True
        return results

    def close(self):
        """Cleanup (no-op for local configurator, kept for API compatibility)."""
        pass
