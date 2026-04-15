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

        # List network interfaces
        try:
            interfaces = []
            for iface in Path("/sys/class/net").iterdir():
                name = iface.name
                if name == "lo":
                    continue
                try:
                    addr_info = (iface / "address").read_text().strip()
                    operstate = (iface / "operstate").read_text().strip()
                    interfaces.append({
                        "name": name,
                        "mac": addr_info,
                        "state": operstate,
                    })
                except Exception:
                    interfaces.append({"name": name, "mac": "", "state": "unknown"})
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
url_rewrite_program {SCRIPTS_DIR}/squid_redirector.sh
url_rewrite_children 5 startup=2 idle=1 concurrency=1
url_rewrite_access allow youtube_domains
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
            "# Hard-block tier (status=block) -- redirect to block page\n"
            f"external_acl_type brainrot_block_check ttl=60 negative_ttl=30 %URI %SRC {SCRIPTS_DIR}/squid_acl_helper.sh block\n"
            "acl brainrot_hard_blocked external brainrot_block_check\n"
            f"deny_info http://{_redirect_ip}:{_port}/blocked brainrot_hard_blocked\n"
            "http_access deny brainrot_hard_blocked\n"
            "\n"
            "# Soft-block tier (status=soft_block) -- redirect to warning page\n"
            f"external_acl_type brainrot_soft_check ttl=60 negative_ttl=30 %URI %SRC {SCRIPTS_DIR}/squid_acl_helper.sh soft_block\n"
            "acl brainrot_soft_blocked external brainrot_soft_check\n"
            f"deny_info http://{_redirect_ip}:{_port}/warning brainrot_soft_blocked\n"
            "http_access deny brainrot_soft_blocked\n"
            "\n"
            "# Delay pool — throttle YouTube CDN (googlevideo.com) per-client\n"
            "# Prevents the browser/app from buffering the whole video before\n"
            "# analysis completes (analysis window is typically 30-120 s).\n"
            "#\n"
            "# Class 2 = per-host limit; aggregate is unlimited so multiple\n"
            "# clients can each reach the per-host cap simultaneously.\n"
            "#\n"
            "#   restore rate : 32 768 B/s  =  256 Kbit/s per client\n"
            "#   bucket size  : 524 288 B   =  512 KB burst allowance\n"
            "#\n"
            "# Smaller bucket limits how much can be pre-buffered before analysis\n"
            "# completes; combined with iptables block (2h) this stops playback.\n"
            "#\n"
            "# Adjust restore rate to balance playback quality vs buffer cap:\n"
            "#   32768   = 256 Kbit/s  (default — very tight pre-buffer)\n"
            "#   131072  = 1 Mbit/s    (SD quality)\n"
            "#   524288  = 4 Mbit/s    (HD quality)\n"
            "#   1048576 = 8 Mbit/s    (full HD)\n"
            "acl youtube_cdn_throttle dstdomain .googlevideo.com\n"
            "delay_pools 1\n"
            "delay_class 1 2\n"
            "delay_parameters 1 -1/-1 32768/524288\n"
            "delay_access 1 allow youtube_cdn_throttle\n"
            "delay_access 1 deny all\n"
            "\n"
            "# Pre-emptive CDN block — deny googlevideo.com while the client's\n"
            "# currently-watching video is still being analysed.  Prevents any\n"
            "# content from being pre-buffered before the verdict is known.\n"
            f"external_acl_type brainrot_cdn_pending ttl=2 negative_ttl=2 %URI %SRC {SCRIPTS_DIR}/squid_cdn_block_helper.sh\n"
            "acl brainrot_client_pending external brainrot_cdn_pending\n"
            "acl youtube_cdn_domains dstdomain .googlevideo.com\n"
            "http_access deny brainrot_client_pending youtube_cdn_domains\n"
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

    # -- iptables Rules ----------------------------------------------------

    def setup_iptables(self) -> Dict[str, Any]:
        """Set up iptables rules for transparent proxy and QUIC blocking."""
        results = {"success": True, "rules_added": 0, "errors": []}
        iface = self.config.network_interface

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

        # Write environment config
        env_path = CONFIG_DIR / "brainrotfilter.env"
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            f"BRAINROT_API={service_url}\n"
            f"GATEWAY_IP=127.0.0.1\n"
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
