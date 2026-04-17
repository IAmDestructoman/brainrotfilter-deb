#!/bin/bash
# First-boot: create an unfiltered L2 bridge (br0) of every physical
# ethernet and DHCP it, so the appliance comes up reachable from both
# the WAN-side and LAN-side of a 2-NIC transparent-bridge deployment
# BEFORE the wizard has been run.
#
# Idempotent: bails out if the wizard has already configured the bridge
# (marked by /etc/brainrotfilter/.wizard_complete) or if we've already
# written the firstboot config (.firstboot_bridge_done).
set -euo pipefail

FLAG_WIZARD=/etc/brainrotfilter/.wizard_complete
FLAG_DONE=/etc/brainrotfilter/.firstboot_bridge_done
NETPLAN_OUT=/etc/netplan/00-brainrot-firstboot.yaml

if [ -f "$FLAG_WIZARD" ]; then
    echo "firstboot_bridge: wizard already completed, skipping"
    exit 0
fi
if [ -f "$FLAG_DONE" ]; then
    echo "firstboot_bridge: already configured, skipping"
    exit 0
fi

# Enumerate physical ethernets (exclude loopback, docker, virtual, bridges).
mapfile -t NICS < <(
    ls /sys/class/net/ 2>/dev/null | while read -r ifc; do
        [ -e "/sys/class/net/$ifc/device" ] || continue
        case "$ifc" in
            lo|docker*|veth*|br*|virbr*|tun*|tap*|wg*) continue ;;
        esac
        echo "$ifc"
    done
)

if [ ${#NICS[@]} -eq 0 ]; then
    echo "firstboot_bridge: no physical ethernets found" >&2
    exit 0
fi

echo "firstboot_bridge: bridging ${NICS[*]} into br0"

mkdir -p /etc/netplan /etc/brainrotfilter

{
    echo "# BrainrotFilter first-boot default — unfiltered bridge of all"
    echo "# physical ethernets, DHCP on br0. Replaced by the wizard's"
    echo "# bridge config when setup completes."
    echo "network:"
    echo "  version: 2"
    echo "  renderer: networkd"
    echo "  ethernets:"
    for n in "${NICS[@]}"; do
        printf '    %s:\n      dhcp4: false\n      dhcp6: false\n      optional: true\n' "$n"
    done
    echo "  bridges:"
    echo "    br0:"
    printf '      interfaces: [%s]\n' "$(IFS=,; echo "${NICS[*]}")"
    echo "      dhcp4: true"
    echo "      dhcp6: false"
    echo "      parameters:"
    echo "        stp: false"
    echo "        forward-delay: 0"
} > "$NETPLAN_OUT"
chmod 600 "$NETPLAN_OUT"

netplan apply || echo "firstboot_bridge: netplan apply returned non-zero (continuing)"

touch "$FLAG_DONE"
echo "firstboot_bridge: done"
