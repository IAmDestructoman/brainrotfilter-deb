#!/bin/bash
# Heartbeat: writes one status line every 60 s until the ISO build finishes.
# Used by the autonomous ISO build flow; NOT shipped in the .deb.
#
# Override via env:
#   BRAINROT_BUILD_DIR=/path/to/build/dir   (default: $HOME/brainrot-iso)
#   BRAINROT_SUDO_PW=password               (only needed if sudo requires it)
: "${BRAINROT_BUILD_DIR:=$HOME/brainrot-iso}"
: "${BRAINROT_SUDO_PW:=password}"
LOG="$BRAINROT_BUILD_DIR/build-full.log"
ISO_DIR="$BRAINROT_BUILD_DIR"
PW="$BRAINROT_SUDO_PW"

read_log() { echo "$PW" | sudo -S tail -200 "$LOG" 2>/dev/null; }
full_tail() { echo "$PW" | sudo -S tail -30 "$LOG" 2>/dev/null; }

shopt -s nullglob
while true; do
    ts=$(date +%H:%M:%S)

    # ISO present? done.
    isos=("$ISO_DIR"/*.iso)
    if [ ${#isos[@]} -gt 0 ]; then
        iso="${isos[0]}"
        size=$(du -h "$iso" | cut -f1)
        echo "[$ts] ISO_READY path=$iso size=$size"
        break
    fi

    size=$(echo "$PW" | sudo -S du -sh "$ISO_DIR/work/chroot" 2>/dev/null | awk '{print $1}')
    phase=$(read_log | grep -oE 'lb_(bootstrap|chroot|binary|source)[_a-z]*' | tail -1)
    running=$(pgrep -f 'build.sh|lb_bootstrap|lb_chroot|lb_binary|debootstrap|mksquashfs|xorriso' | wc -l)
    last_err=$(full_tail | grep -E '^E:|error status|gpg:.*failed|cannot remove' | tail -1)

    echo "[$ts] phase=${phase:-?} chroot=${size:-0} procs=$running err=${last_err:-none}"

    # Build finished? (exited, BUILD_EXIT line written)
    if [ "$running" -eq 0 ] && echo "$PW" | sudo -S grep -q BUILD_EXIT "$LOG" 2>/dev/null; then
        exit_line=$(echo "$PW" | sudo -S grep BUILD_EXIT "$LOG" | tail -1)
        isos2=("$ISO_DIR"/*.iso)
        if [ ${#isos2[@]} -gt 0 ]; then
            echo "[$ts] BUILD_FINISHED_SUCCESS $exit_line"
        else
            echo "[$ts] BUILD_FINISHED_FAIL $exit_line"
        fi
        break
    fi

    sleep 60
done
