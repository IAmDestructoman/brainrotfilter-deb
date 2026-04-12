#!/bin/sh
# BrainrotFilter -- Squid URL Rewrite Program (POSIX shell, Linux)
#
# Squid config:
#   url_rewrite_program /usr/local/bin/brainrotfilter/squid_redirector.sh
#   url_rewrite_children 5 startup=2 idle=1 concurrency=0
#
# Protocol (input):   <ID> <URL> <client_ip>/<fqdn> <ident> <method> [kvpairs]
# Protocol (output):  <ID> OK [rewrite-url=<url>]

set -u

# -- Configuration -----------------------------------------------------------
CONFIG_FILE="/etc/brainrotfilter/brainrotfilter.env"
if [ -f "$CONFIG_FILE" ]; then
    . "$CONFIG_FILE"
fi
BRAINROT_API="${BRAINROT_API:-http://127.0.0.1:8199}"
CURL_TIMEOUT=2

log() {
    echo "[brainrot-redirector] $*" >&2
}

# -- Extract YouTube video ID from URL ----------------------------------------
# Returns video ID on stdout, empty string if not a YouTube URL.
extract_video_id() {
    _url="$1"

    case "$_url" in
        *youtube.com/watch*)
            # Extract v= parameter
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
        *)
            _vid=""
            ;;
    esac

    echo "$_vid"
}

# -- Parse JSON field (lightweight, no jq dependency) -------------------------
# Usage: json_field "field_name" "$json_string"
json_field() {
    _field="$1"
    _json="$2"
    echo "$_json" | sed -n 's/.*"'"$_field"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
}

# -- Main loop ----------------------------------------------------------------
log "started -- API=${BRAINROT_API}"

while IFS= read -r line; do
    # Parse Squid rewrite protocol
    # Format: <ID> <URL> <client_ip>/<fqdn> <ident> <method> [kvpairs]
    id="${line%% *}"
    rest="${line#* }"
    url="${rest%% *}"
    rest="${rest#* }"
    client_info="${rest%% *}"
    client_ip="${client_info%%/*}"

    # Extract video ID
    video_id=$(extract_video_id "$url")

    # Non-YouTube URL -- pass through immediately
    if [ -z "$video_id" ]; then
        echo "${id} OK"
        continue
    fi

    # Call the BrainrotFilter API to check this video
    response=$(curl -s --max-time "$CURL_TIMEOUT" \
        -X POST "${BRAINROT_API}/api/check" \
        -H "Content-Type: application/json" \
        -d "{\"video_id\":\"${video_id}\"}" 2>/dev/null)

    # On curl failure or timeout -- pass through
    if [ $? -ne 0 ] || [ -z "$response" ]; then
        log "API timeout/error for ${video_id} -- passing through"
        echo "${id} OK"
        continue
    fi

    action=$(json_field "action" "$response")
    redirect_url=$(json_field "redirect_url" "$response")
    reason=$(json_field "reason" "$response")

    case "$action" in
        block|soft_block)
            if [ -n "$redirect_url" ]; then
                log "${action} ${video_id} -> ${redirect_url}"
                echo "${id} OK rewrite-url=${redirect_url}"
            else
                # Fallback redirect to block page
                log "${action} ${video_id} -> fallback block page"
                echo "${id} OK rewrite-url=${BRAINROT_API}/block?video_id=${video_id}"
            fi
            ;;
        *)
            echo "${id} OK"

            # If the video is unknown, queue it for background analysis
            if [ "$reason" = "not_blocked" ]; then
                curl -s --max-time "$CURL_TIMEOUT" \
                    -X POST "${BRAINROT_API}/api/analyze" \
                    -H "Content-Type: application/json" \
                    -d "{\"video_id\":\"${video_id}\",\"client_ip\":\"${client_ip}\"}" \
                    >/dev/null 2>&1 &
                log "queued analysis for ${video_id} (client=${client_ip})"
            fi
            ;;
    esac
done

log "stdin closed -- exiting"
