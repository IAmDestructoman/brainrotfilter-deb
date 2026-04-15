#!/bin/bash
# apply_squid_config.sh
# ---------------------
# Privileged helper that runs as root (via brainrotfilter-squid-apply.service)
# to add the BrainrotFilter include directive to /etc/squid/squid.conf.
#
# The main brainrotfilter service (which runs as an unprivileged system user
# with NoNewPrivileges=true) cannot write to /etc/squid/squid.conf directly.
# Instead, it writes the desired include path to a request file and then
# asks systemd to start this oneshot service via a polkit rule.
#
# Request file format (written by linux_configurator.py):
#   /var/lib/brainrotfilter/squid_include_path
#   Contains a single line: the absolute path to the brainrot squid snippet.

set -euo pipefail

REQUEST_FILE="/var/lib/brainrotfilter/squid_include_path"
SQUID_CONF="/etc/squid/squid.conf"
LOG_TAG="brainrotfilter-squid-apply"

log() { logger -t "$LOG_TAG" "$*"; echo "$*"; }

if [ ! -f "$REQUEST_FILE" ]; then
    log "No request file at $REQUEST_FILE — nothing to do."
    exit 0
fi

CONF_SNIPPET=$(cat "$REQUEST_FILE" | tr -d '\n\r' | head -c 256)

# Basic path safety check
if [[ "$CONF_SNIPPET" != /etc/brainrotfilter/* ]]; then
    log "ERROR: Requested path '$CONF_SNIPPET' is outside /etc/brainrotfilter — refusing."
    exit 1
fi

if [ ! -f "$SQUID_CONF" ]; then
    log "ERROR: $SQUID_CONF not found — is Squid installed?"
    exit 1
fi

INCLUDE_LINE="include $CONF_SNIPPET"

if grep -qF "$CONF_SNIPPET" "$SQUID_CONF"; then
    log "Include directive already present in $SQUID_CONF — nothing to do."
else
    # Append the include directive
    printf '\n# BrainrotFilter\n%s\n' "$INCLUDE_LINE" >> "$SQUID_CONF"
    log "Added '$INCLUDE_LINE' to $SQUID_CONF"
fi

# Validate the resulting config
if squid -k parse 2>&1 | grep -q "FATAL"; then
    log "ERROR: squid -k parse reported a FATAL error after writing config."
    # Remove the line we just added to avoid leaving squid broken
    sed -i "/# BrainrotFilter/{N;N;d}" "$SQUID_CONF" || true
    exit 2
fi

# Clean up request file
rm -f "$REQUEST_FILE"
log "Squid configuration applied successfully."
exit 0
