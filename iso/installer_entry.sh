#!/bin/bash
# Installer-only entry point. Runs on tty1 autologin in the live ISO
# — no configuration UI, no network wizard, nothing that touches the
# appliance services. Just install-to-disk and a couple of escape
# hatches. After install the operator reboots and the installed
# system brings up the real TUI from the golden image.
set +e
while true; do
    clear
    cat <<'BANNER'
================================================================
  BrainrotFilter Appliance Installer
================================================================

  1) Install to disk
  2) Wipe disk (recovery)
  3) Drop to shell
  4) Reboot
  5) Shutdown

BANNER
    read -rp "  Select: " pick
    case "$pick" in
        1)
            sudo -n /usr/lib/brainrotfilter/scripts/install_to_disk.sh
            echo
            read -rp "  Press Enter to return to menu: " _
            ;;
        2)
            sudo -n /usr/lib/brainrotfilter/scripts/wipe_disk.sh
            echo
            read -rp "  Press Enter to return to menu: " _
            ;;
        3)
            echo "  Type 'exit' to return to this menu."
            bash --login
            ;;
        4)
            echo "  Rebooting..."
            # Flush pending writes, drop caches, sync — then try
            # progressively more forceful reboot paths. On Hyper-V
            # after a half-install the normal systemctl reboot
            # sometimes hangs because services are in bad states.
            sudo -n sync 2>/dev/null
            sudo -n systemctl reboot 2>/dev/null
            sleep 5
            sudo -n systemctl reboot --force 2>/dev/null
            sleep 5
            sudo -n reboot -f 2>/dev/null
            sleep 5
            # Last resort: direct sysrq trigger.
            sudo -n bash -c 'echo b > /proc/sysrq-trigger' 2>/dev/null
            sleep 10
            ;;
        5)
            echo "  Shutting down..."
            sudo -n sync 2>/dev/null
            sudo -n systemctl poweroff 2>/dev/null
            sleep 5
            sudo -n systemctl poweroff --force 2>/dev/null
            sleep 5
            sudo -n poweroff -f 2>/dev/null
            sleep 5
            sudo -n bash -c 'echo o > /proc/sysrq-trigger' 2>/dev/null
            sleep 10
            ;;
        *)
            ;;
    esac
done
