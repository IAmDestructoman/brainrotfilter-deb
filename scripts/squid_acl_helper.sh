#!/bin/sh
# BrainrotFilter -- Squid External ACL Helper (POSIX shell, Linux)
#
# Squid config:
#   external_acl_type brainrot_check ttl=60 %URI /usr/lib/brainrotfilter/scripts/squid_acl_helper.sh
#   acl brainrot_blocked external brainrot_check
#   http_access deny brainrot_blocked
#
# Protocol (input):  <URL>   (one per line)
# Protocol (output): OK      (blocked -- deny access)
#                    ERR     (not blocked -- allow access)

set -u

# -- Configuration -----------------------------------------------------------
CONFIG_FILE="/etc/brainrotfilter/brainrotfilter.env"
if [ -f "$CONFIG_FILE" ]; then
    . "$CONFIG_FILE"
fi
BRAINROT_API="${BRAINROT_API:-http://127.0.0.1:8199}"
CURL_TIMEOUT=2

log() {
    echo "[brainrot-acl] $*" >&2
}

# -- Extract YouTube video ID from URL ----------------------------------------
extract_video_id() {
    _url="$1"

    case "$_url" in
        *youtube.com/watch*)
            _vid="${_url#*v=}"
            _vid="${_vid%%&*}"
            _vid="${_vid%%#*}"
            ;;
        *youtube.com/shorts/*)
            _vid="${_url#*youtube.com/shorts/}"
            _vid="${_vid%%\?*}"
            _vid="${_vid%%&*}"
            _vid="${_vid%%#*}"
            _vid="${_vid%%/*}"
            ;;
        *youtube.com/embed/*)
            _vid="${_url#*youtube.com/embed/}"
            _vid="${_vid%%\?*}"
            _vid="${_vid%%&*}"
            _vid="${_vid%%#*}"
            _vid="${_vid%%/*}"
            ;;
        *youtu.be/*)
            _vid="${_url#*youtu.be/}"
            _vid="${_vid%%\?*}"
            _vid="${_vid%%&*}"
            _vid="${_vid%%#*}"
            _vid="${_vid%%/*}"
            ;;
        *ytimg.com/vi_webp/*)
            _vid="${_url#*ytimg.com/vi_webp/}"
            _vid="${_vid%%/*}"
            _vid="${_vid%%\?*}"
            ;;
        *ytimg.com/vi/*)
            _vid="${_url#*ytimg.com/vi/}"
            _vid="${_vid%%/*}"
            _vid="${_vid%%\?*}"
            ;;
        *ytimg.com/sb/*)
            _vid="${_url#*ytimg.com/sb/}"
            _vid="${_vid%%/*}"
            _vid="${_vid%%\?*}"
            ;;
        *)
            _vid=""
            ;;
    esac

    echo "$_vid"
}

# -- Parse JSON field ---------------------------------------------------------
json_field() {
    _field="$1"
    _json="$2"
    echo "$_json" | sed -n 's/.*"'"$_field"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
}

# -- Main loop ----------------------------------------------------------------
log "started -- API=${BRAINROT_API}"

while IFS= read -r url; do
    # Strip leading/trailing whitespace
    url=$(echo "$url" | tr -d '\r')

    video_id=$(extract_video_id "$url")

    # Non-YouTube URL -- allow
    if [ -z "$video_id" ]; then
        echo "ERR"
        continue
    fi

    # Call the BrainrotFilter API
    response=$(curl -s --max-time "$CURL_TIMEOUT" \
        -X POST "${BRAINROT_API}/api/check" \
        -H "Content-Type: application/json" \
        -d "{\"video_id\":\"${video_id}\"}" 2>/dev/null)

    # On failure -- allow (fail open)
    if [ $? -ne 0 ] || [ -z "$response" ]; then
        log "API timeout/error for ${video_id} -- allowing"
        echo "ERR"
        continue
    fi

    action=$(json_field "action" "$response")

    case "$action" in
        block|soft_block)
            log "blocked ${video_id}"
            echo "OK"
            ;;
        *)
            echo "ERR"
            ;;
    esac
done

log "stdin closed -- exiting"
