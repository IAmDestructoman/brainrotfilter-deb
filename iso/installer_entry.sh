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
            sudo -n systemctl reboot
            sleep 10
            ;;
        5)
            sudo -n systemctl poweroff
            sleep 10
            ;;
        *)
            ;;
    esac
done
