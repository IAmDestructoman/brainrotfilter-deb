#!/bin/sh
# BrainrotFilter -- CDN Pre-emptive Block Helper (Squid external_acl_type)
#
# Input:  %URI %SRC   (URL is ignored; we key on client source IP)
# Output: OK  -- client has a pending video analysis -> DENY this CDN request
#         ERR -- client is clear -> ALLOW
#
# Purpose: deny googlevideo.com / ytimg.com CDN requests while the client's
# currently-watching video is still being analyzed, so no data can be
# pre-buffered before the verdict.
#
# Paired with squid.conf:
#   external_acl_type brainrot_cdn_pending ttl=2 %URI %SRC .../squid_cdn_block_helper.sh
#   acl brainrot_client_pending external brainrot_cdn_pending
#   acl youtube_cdn_domains dstdomain .googlevideo.com
#   http_access deny brainrot_client_pending youtube_cdn_domains

set -u

CONFIG_FILE="/etc/brainrotfilter/brainrotfilter.env"
if [ -f "$CONFIG_FILE" ]; then
    . "$CONFIG_FILE"
fi
BRAINROT_API="${BRAINROT_API:-http://127.0.0.1:8199}"
CURL_TIMEOUT=1

log() {
    echo "[brainrot-cdn] $*" >&2
}

log "started -- API=${BRAINROT_API}"

while IFS= read -r line; do
    line=$(echo "$line" | tr -d '\r')

    # Squid now sends only %SRC (one field). Strip any whitespace just in case.
    src_ip=$(echo "$line" | awk '{print $1}')
    if [ -z "$src_ip" ] || [ "$src_ip" = "-" ]; then
        echo "ERR"
        continue
    fi

    response=$(curl -s --max-time "$CURL_TIMEOUT" \
        "${BRAINROT_API}/api/client-pending?ip=${src_ip}" 2>/dev/null)

    # On API failure -- fail open (allow)
    if [ -z "$response" ]; then
        echo "ERR"
        continue
    fi

    case "$response" in
        *'"pending":true'*)
            echo "OK"   # ACL matches -> deny request
            ;;
        *)
            echo "ERR"
            ;;
    esac
done

log "stdin closed -- exiting"
