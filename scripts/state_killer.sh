#!/bin/sh
# BrainrotFilter -- State Killer (POSIX shell, Linux)
#
# Kills active conntrack entries from a client IP to known Google/YouTube CDN
# ranges. This terminates in-progress video streams immediately after a block
# decision.
#
# Uses conntrack (netfilter connection tracking) instead of pfctl.
#
# Usage: state_killer.sh <client_ip>

set -u

# -- Google / YouTube CDN IP prefixes ----------------------------------------
# These cover the major Google CDN ranges that serve YouTube video content.
# Update periodically -- check: dig @8.8.8.8 +short redirector.googlevideo.com
GOOGLE_PREFIXES="142.250. 172.217. 216.58. 74.125. 172.253. 173.194. 209.85. 64.233. 108.177. 142.251."

log() {
    echo "[brainrot-state-killer] $*" >&2
}

if [ $# -lt 1 ]; then
    echo "Usage: $0 <client_ip>" >&2
    exit 1
fi

CLIENT_IP="$1"
KILLED=0

log "killing YouTube CDN connections for client ${CLIENT_IP}"

# Check if conntrack is available
if ! command -v conntrack >/dev/null 2>&1; then
    log "ERROR: conntrack not found. Install conntrack-tools package."
    exit 1
fi

# List connections from client and find YouTube CDN destinations
conntrack -L -s "$CLIENT_IP" -p tcp 2>/dev/null | while IFS= read -r line; do
    # conntrack output lines look like:
    # tcp  6 300 ESTABLISHED src=192.168.1.5 dst=142.250.80.46 sport=54321 dport=443 ...

    # Extract destination IP
    dst_ip=$(echo "$line" | sed -n 's/.*dst=\([0-9.]*\).*/\1/p' | head -1)

    if [ -z "$dst_ip" ]; then
        continue
    fi

    # Check if destination matches any Google/YouTube prefix
    for prefix in $GOOGLE_PREFIXES; do
        case "$dst_ip" in
            ${prefix}*)
                log "killing connection: ${CLIENT_IP} -> ${dst_ip}"
                conntrack -D -s "$CLIENT_IP" -d "$dst_ip" -p tcp 2>/dev/null
                KILLED=$((KILLED + 1))
                break
                ;;
        esac
    done
done

log "done -- killed ${KILLED} connection group(s) for ${CLIENT_IP}"
