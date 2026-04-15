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
    # Insert the include directive immediately before the first
    # 'http_access deny all' line so our allow rules take effect before
    # Squid's default catch-all deny.  Fall back to appending if that
    # sentinel line is not found (non-standard squid.conf).
    if grep -qE '^http_access deny all$' "$SQUID_CONF"; then
        sed -i "0,/^http_access deny all$/{s|^http_access deny all$|# BrainrotFilter\n${INCLUDE_LINE}\n\nhttp_access deny all|}" "$SQUID_CONF"
    else
        printf '\n# BrainrotFilter\n%s\n' "$INCLUDE_LINE" >> "$SQUID_CONF"
    fi
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

# -- Write conf.d snippet if requested ------------------------------------
CONFD_REQUEST="/var/lib/brainrotfilter/squid_confd_content"
CONFD_FILE="/etc/squid/conf.d/brainrotfilter.conf"

if [ -f "$CONFD_REQUEST" ]; then
    mkdir -p /etc/squid/conf.d
    cp "$CONFD_REQUEST" "$CONFD_FILE"
    chmod 644 "$CONFD_FILE"
    rm -f "$CONFD_REQUEST"
    log "Wrote $CONFD_FILE from request file."
fi

log "Squid configuration applied successfully."
exit 0
