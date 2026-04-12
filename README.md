# BrainrotFilter for Linux (Debian/Ubuntu)

A network-level YouTube content filter that analyzes videos using keyword matching, scene detection, audio analysis, and engagement metrics. Integrates with Squid proxy for transparent HTTPS interception on Linux systems.

## Overview

BrainrotFilter sits between your network clients and YouTube, transparently analyzing video content in real-time. When it detects "brainrot" content (low-quality, attention-grabbing videos), it can block, warn, or monitor based on configurable thresholds.

This is the **Linux/Debian** port of BrainrotFilter (originally built for pfSense/FreeBSD).

## Features

- **Real-time YouTube analysis** via Squid SSL bump (peek-stare-bump)
- **Multiple analysis engines**: keyword, scene, audio, comments, engagement, thumbnail
- **Configurable thresholds** with monitor/soft-block/block tiers
- **Web admin dashboard** on port 8199
- **Setup wizard** for guided configuration
- **iptables integration** for transparent proxy and QUIC blocking
- **conntrack integration** for killing active video streams mid-playback
- **Community keyword sharing** for collaborative blocklists
- **Channel auto-escalation** when a channel has too many flagged videos

## Requirements

- Debian 12+ or Ubuntu 22.04+
- Python 3.9+
- Squid 5.0+ (with SSL support: `squid-openssl`)
- iptables, conntrack-tools
- openssl, curl, ffmpeg
- YouTube Data API v3 key

## Installation

### From .deb package (recommended)

```bash
sudo dpkg -i brainrotfilter_1.0.0-1_all.deb
sudo apt-get install -f  # install dependencies
```

### From source

```bash
git clone https://github.com/IAmDestructoman/brainrotfilter-deb.git
cd brainrotfilter-deb
make build-deb
sudo dpkg -i ../brainrotfilter_1.0.0-1_all.deb
```

## Quick Start

1. Install the package
2. Open `http://<your-ip>:8199` in a browser
3. The setup wizard will guide you through:
   - YouTube API key configuration
   - Network interface selection
   - Detection threshold tuning
   - Squid/SSL/iptables setup

## Architecture

```
Client -> iptables REDIRECT -> Squid (SSL bump) -> BrainrotFilter API -> YouTube
                                  |
                          squid_redirector.sh
                          squid_acl_helper.sh
                                  |
                          BrainrotFilter Service (port 8199)
                            - FastAPI + Uvicorn
                            - Keyword analyzer
                            - Scene analyzer (OpenCV)
                            - Audio analyzer (librosa/vosk)
                            - Comment/engagement analyzer
```

## Configuration

- **Service config**: `/etc/brainrotfilter/`
- **Database**: `/var/lib/brainrotfilter/brainrotfilter.db`
- **Logs**: `/var/log/brainrotfilter/`
- **Shell helpers**: `/usr/local/bin/brainrotfilter/`
- **Web assets**: `/usr/share/brainrotfilter/www/`
- **Systemd service**: `brainrotfilter.service`

## Service Management

```bash
sudo systemctl status brainrotfilter
sudo systemctl restart brainrotfilter
sudo journalctl -u brainrotfilter -f
```

## Building

```bash
make build-deb    # Build .deb package
make test         # Run tests
make lint         # Run linter
make clean        # Clean build artifacts
```

## License

MIT License. See [LICENSE](LICENSE) for details.
