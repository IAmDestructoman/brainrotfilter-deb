# BrainrotFilter — Known Issues / Deferred Work

## ssl_pinning step fails: `/etc/hosts` permission denied

The wizard's ssl-pinning-bypass step appends two lines to `/etc/hosts`
to null-route YouTube's mobile-app API endpoints. `/etc/hosts` is
`644 root:root`; the `brainrotfilter` service user can't write it,
and `ProtectSystem=strict` is not the cause (ReadWritePaths covers
the path — it's plain Linux file perms).

**Options for the fix:**
1. Give `brainrotfilter` an ACL on `/etc/hosts` via `postinst`:
   `setfacl -m u:brainrotfilter:rw /etc/hosts` (cleanest, no sudo/polkit).
2. Ship a tiny setuid helper at `/usr/lib/brainrotfilter/scripts/write_hosts.sh`
   that appends the fixed bypass lines only.
3. Drop the `/etc/hosts` approach entirely — serve a DNS-resolving local
   stub (e.g. systemd-resolved drop-in) that returns `0.0.0.0` for the
   mobile app hostnames.

Non-blocking: desktop/browser interception still works without this;
only YouTube Android/iOS apps would bypass via pinning.

## install_to_disk.sh preservation gaps

`scripts/install_to_disk.sh` currently rsyncs:

- `/etc/brainrotfilter` — wizard flag, env, helper dir
- `/var/lib/brainrotfilter` — SQLite DB, CA cert, session state
- `/etc/systemd/network` — bridge + management IP config
- `/etc/netplan` — netplan files (if any survived)

But it does **not** preserve:

- `/etc/ssh/` — SSH host keys *(intentional — regen on new box)* AND
  the `sshd_config.d/99-brainrotfilter.conf` overrides *(fine — baked
  into squashfs)*. But: the operator's **enabled/disabled SSH state**
  isn't preserved — install comes up with SSH masked again.
- `/etc/shadow`, `/etc/passwd`, `/etc/group`, `/etc/gshadow` — the
  **root password** set via TUI option 3 or 7 is lost; installed
  system has passwordless root again.
- `/etc/sudoers.d/` — any custom rules.
- `/etc/systemd/system/*.target.wants/` symlinks — "which units are
  enabled" state, including ssh + any user-enabled extras.
- `/etc/iptables/` — persisted iptables rules. Wizard writes them;
  installed system needs them to re-apply interception on first boot.

**Fix:** extend the rsync loop in `install_to_disk.sh` to cover the
above paths. Special-case `/etc/ssh`: keep `sshd_config.d/` and the
"is enabled" symlinks but **delete host keys** on the target so
each install regenerates its own fingerprint on first enable.
