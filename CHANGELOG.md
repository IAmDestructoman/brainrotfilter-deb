# Changelog

## 1.0.0 (2026-04-12)

Initial release of BrainrotFilter for Linux (Debian/Ubuntu).

### Added
- Complete port from pfSense/FreeBSD to Linux
- Local Linux configurator replacing SSH-based pfSense configurator
- openssl-based CA certificate generation
- iptables rules for transparent proxy (port 80->3128, 443->3129)
- iptables QUIC/HTTP3 blocking (UDP 443/80)
- conntrack-based connection killing (replacing pfctl)
- systemd service integration
- Debian package structure with postinst/prerm scripts
- Web-based setup wizard for Linux
- Web-based uninstall page
- POSIX shell helpers for Squid integration
- Full web admin dashboard
- GitHub Actions CI and release workflows
