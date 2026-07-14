# SD-card / USB media ingest station

Copy footage off a bank of USB card readers, hash-verify every file, upload it
to the cloud, and only wipe a card once a human confirms — with live per-card
status on a small LCD.

The system splits into a **dumb display** and a **smart host**, and the risky
copy/verify/upload is delegated to **rclone**:

- **Device** (`device/`, `app/`) — a **Waveshare RP2350-LCD-1.47-A** running an
  LVGL UI. It renders the four-stage progress bars the host sends over a serial
  line protocol, and sends back `confirm <i>` when the operator approves a wipe
  (via the board's BOOTSEL button).
- **Ingest daemon** (`host/ingest*.py`) — discovers readers in physical order,
  and per card runs `rclone copy` → `rclone check` into
  `dest_base/<uuid>/<ingest_date>/`, then waits for a confirm to wipe. It is the
  only thing that deletes, and only on an explicit confirm.
- **Uploader** (`host/uploader.py`) — a *separate* process that pushes verified
  ingest dirs to a cloud remote (rclone) and proves it by re-checking against
  the remote's own hashes. Decoupled, so a card can be wiped and gone while its
  local copy is still uploading.
- **Simulator** (`sim/`) — the exact device UI in an SDL window, no hardware.

The bar climbs through four colourblind-safe stages: **uncopied → copied →
verified → uploaded**. See `ARCHITECTURE.md` for the full split + protocol and
`DECISIONS.md` for the running rationale.

## Quick start (Nix)

Requires Nix with flakes enabled (`experimental-features = nix-command flakes`),
and `rclone` (provided by the flake apps).

```bash
# Watch the whole ingest lifecycle with no hardware: the real daemon drives the
# simulator with fake cards (copy -> verify -> pending -> wipe).
nix run .#ingest -- --dry-run | nix run .#sim

# The real thing:
nix run .#ingest   -- --config /etc/ingest.toml   # discover, copy+verify, wipe
nix run .#uploader -- --config /etc/ingest.toml   # push verified dirs to cloud

# Build + flash the on-device display firmware (-> ./result/firmware.uf2).
nix build .#firmware-ui && nix run .#flash

# Install both as systemd services (bakes paths, drops /etc/ingest.toml).
nix run .#install-service
```

In the simulator, **SPACE** stands in for the board's BOOTSEL button (hold past
600 ms = long press); **ESC** quits.

## Wipe safety

Deletion never happens automatically. A card is wiped only after every file is
copied *and* hash-verified *and* the operator sends `confirm <i>` — and even
then it defaults to a logged dry run. Real deletion needs **both** `[wipe]
enabled = true` in the config **and** the environment variable
`INGEST_ENABLE_WIPE=1` (no CLI flag, so a systemd unit arms it deliberately).
The wipe also re-checks each source (size+mtime) right before deleting it.

## Cloud upload

The uploader pushes each verified `dest_base/<uuid>/<date>/` to `[remote] base`
(an rclone destination like `b2:bucket/ingest`, `gdrive:ingest`, or a second
disk), then runs `rclone check` against the remote — which reads the backend's
stored **SHA1** from object metadata, so it confirms the bytes are really up
there **without downloading**. Proof is recorded in `REMOTE_SHA1SUMS` and the
dir's `metadata.json` flips to `uploaded`. The remote + credentials come from
rclone's own config (`rclone config`).

## Board doesn't show up

The tools find the board by its USB id (`2e8a`). If `nix run .#flash` says "no
devices in BOOTSEL" or no `2e8a` serial port appears:

- Plug the board **directly into the computer**, not through a USB hub.
- Use a **data** USB-C cable, not a charge-only one.
- For flashing, hold the **BOOT** button while plugging in to force BOOTSEL mode
  (it appears as an `RP2350` mass-storage drive; you can also drag the `.uf2`
  onto it).
- Once firmware is running it enumerates as a serial port (`/dev/ttyACM*`).

## Serial permissions

`picotool` may need udev rules or `sudo`; the CDC port (`/dev/ttyACM*`) needs
your user in the `dialout` group (log out/in after `usermod -aG dialout $USER`).
