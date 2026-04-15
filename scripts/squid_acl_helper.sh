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
        *youtube.com/api/stats/watchtime*|*youtube.com/api/stats/qoe*|*youtube.com/api/stats/playback*)
            _vid="${_url#*docid=}"
            _vid="${_vid%%&*}"
            _vid="${_vid%%#*}"
            if [ ${#_vid} -ne 11 ]; then _vid=""; fi
            ;;
        *youtube.com/api/timedtext*)
            _vid="${_url#*v=}"
            _vid="${_vid%%&*}"
            _vid="${_vid%%#*}"
            if [ ${#_vid} -ne 11 ]; then _vid=""; fi
            ;;
        *youtubei/v1/player*|*youtubei/v1/next*)
            case "$_url" in
                *videoId=*)
                    _vid="${_url#*videoId=}"
                    _vid="${_vid%%&*}"
                    ;;
                *)
                    _vid=""
                    ;;
            esac
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

# -- Filter mode (optional first argument) ------------------------------------
# When two external_acl_type instances are declared — one for hard blocks and
# one for soft blocks — pass a mode argument so each returns OK only for the
# relevant status tier:
#   "block"      -- return OK only for status=block
#   "soft_block" -- return OK only for status=soft_block
#   (omitted)    -- return OK for both (default, backwards-compatible)
FILTER_MODE="${1:-both}"

# -- Main loop ----------------------------------------------------------------
log "started -- API=${BRAINROT_API} mode=${FILTER_MODE}"

while IFS= read -r line; do
    # Strip carriage returns
    line=$(echo "$line" | tr -d '\r')

    # Squid sends: URL SRC_IP  (space-separated, from %URI %SRC)
    url="${line%% *}"
    src_ip="${line##* }"
    # If there was no space (only URL), src_ip equals url
    if [ "$src_ip" = "$url" ]; then
        src_ip=""
    fi

    video_id=$(extract_video_id "$url")

    # Non-YouTube URL -- allow
    if [ -z "$video_id" ]; then
        echo "ERR"
        continue
    fi

    # Build JSON body — include client_ip when available
    if [ -n "$src_ip" ]; then
        json_body="{\"video_id\":\"${video_id}\",\"client_ip\":\"${src_ip}\"}"
    else
        json_body="{\"video_id\":\"${video_id}\"}"
    fi

    # Call the BrainrotFilter API
    response=$(curl -s --max-time "$CURL_TIMEOUT" \
        -X POST "${BRAINROT_API}/api/check" \
        -H "Content-Type: application/json" \
        -d "$json_body" 2>/dev/null)

    # On failure -- allow (fail open)
    if [ $? -ne 0 ] || [ -z "$response" ]; then
        log "API timeout/error for ${video_id} -- allowing"
        echo "ERR"
        continue
    fi

    action=$(json_field "action" "$response")

    case "$action" in
        block)
            if [ "$FILTER_MODE" = "block" ] || [ "$FILTER_MODE" = "both" ]; then
                log "hard-blocked ${video_id}"
                echo "OK"
            else
                echo "ERR"
            fi
            ;;
        soft_block)
            if [ "$FILTER_MODE" = "soft_block" ] || [ "$FILTER_MODE" = "both" ]; then
                log "soft-blocked ${video_id}"
                echo "OK"
            else
                echo "ERR"
            fi
            ;;
        *)
            echo "ERR"
            ;;
    esac
done

log "stdin closed -- exiting"
