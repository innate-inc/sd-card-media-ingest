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

Requires Nix with flakes enabled (`experimental-features = nix-command flakes`).
rclone/pyserial/etc. come with the flake apps — nothing else to install.

### Quickstart: test / develop (no hardware)

```bash
# Watch the whole lifecycle in the simulator: the real daemon drives the sim
# with fake cards (copy -> verify -> pending -> wipe). SPACE = BOOTSEL button
# (hold = long press), ESC quits.
nix run .#ingest -- --dry-run | nix run .#sim

# ...and auto-confirm the wipe so it runs hands-free:
nix run .#ingest -- --dry-run --auto-confirm 2 | nix run .#sim

nix flake check          # run the test suite (proto, copier+uploader, renders)
```

**Dev shell** — `nix develop` drops you into a shell with the whole toolchain
(arm-none-eabi + cmake + Pico SDK for the firmware, python + pyserial, rclone,
picotool). From there you can run things directly:

```bash
nix develop
python3 tests/test_ingest.py                     # host tests (needs rclone -- it's here)
python3 host/ingest.py --dry-run | nix run .#sim  # run the daemon from source
cmake -S device -B build -DLVGL_DIR=... && cmake --build build   # build fw by hand
```

### Quickstart: install / setup / config (on the box)

```bash
# Config + secrets live in THIS repo dir; run everything from here (the systemd
# units get WorkingDirectory baked in, so keep the repo at a stable path).

# 1. One-time cloud remote for the uploader (skip if you only back up locally):
nix run .#rclone -- config                # make a remote "b2" -> ./rclone.conf

# 2. Edit ./ingest.toml — the three things you must set:
#      [dest]   base    = "/media/.../ingest/"   # where copies land
#      [remote] base    = "b2:my-bucket/ingest"  # rclone dest ("" = local only)
#      [hub]    path_prefix = "..."              # ls /dev/disk/by-path | grep usb
#    Leave [wipe] enabled = false until you trust it (dry-run logging).
$EDITOR ingest.toml

# 3. Flash the display firmware, install + start the services:
nix build .#firmware-ui && nix run .#flash
nix run .#install-service                  # units point at $PWD/ingest.toml
sudo systemctl enable --now ingest uploader
journalctl -fu ingest                      # watch it work

# 4. When ready to REALLY delete cards after backup, arm the wipe:
#    set [wipe] enabled = true in ./ingest.toml, then: sudo systemctl restart ingest
#    (it logs "wipe ARMED" loudly at startup).
```

## Wipe safety

Deletion never happens automatically. A card is wiped only after every file is
copied *and* hash-verified *and* the operator sends `confirm <i>` — and even
then it defaults to a logged dry run. Real deletion is armed only by `[wipe]
enabled = true` in `ingest.toml` (the daemon logs `wipe ARMED` loudly at
startup). The wipe also re-checks each source (size+mtime) right before deleting
it, and every action is logged (`journalctl -u ingest`).

## Cloud upload

The uploader pushes each verified `dest_base/<uuid>/<date>/` to `[remote] base`
(an rclone destination like `b2:bucket/ingest`, `gdrive:ingest`, or a second
disk), then runs `rclone check` against the remote — which reads the backend's
stored **SHA1** from object metadata, so it confirms the bytes are really up
there **without downloading**. Proof is recorded in `REMOTE_SHA1SUMS`, and an
`uploaded.json` in the dir marks it done (the copier owns `metadata.json`, the
uploader owns `uploaded.json` — one writer each). The remote + credentials come
from rclone's own config (`rclone config`).

### Set up Backblaze B2 with rclone

```bash
# In the Backblaze web console:
#   1. Create a bucket (e.g. "myco-ingest"), Private.
#   2. Application Keys -> Add a New Application Key, restricted to that bucket,
#      Read and Write. Copy the keyID and applicationKey (shown only once).

# Configure the rclone remote (writes ./rclone.conf), interactively:
nix run .#rclone -- config
#   n) New remote          name> b2
#   Storage>               b2            # Backblaze B2
#   account (Account ID or Application Key ID)>  <keyID>
#   key (Application Key)>                       <applicationKey>
#   ...accept defaults, y) keep, q) quit

# ...or in one shot:
nix run .#rclone -- config create b2 b2 account <keyID> key <applicationKey>

# Verify:
nix run .#rclone -- lsd b2:              # lists your buckets
nix run .#rclone -- ls  b2:myco-ingest   # (empty at first)

# Then in /etc/ingest.toml:
#   [remote]
#   base = "b2:myco-ingest/ingest"
```

B2 stores each object's **SHA1** in metadata (rclone supplies it even for large
multipart files), so `rclone check`/`sha1sum` verify the upload from metadata
alone — no download. That's why the pipeline hashes with SHA1.

**Where the remote config lives:** `nix run .#rclone -- …` is just rclone with
`RCLONE_CONFIG` pointed at **`./rclone.conf`** in the project dir (gitignored —
it holds secrets), so `nix run .#rclone -- config` sets up your remote right
there. The `ingest`/`uploader` apps auto-use the same file when run from that
dir, and `nix run .#install-service` bakes that dir into the uploader unit as
`WorkingDirectory` + `RCLONE_CONFIG` — so no `/etc`, no root config. Keep the
repo at a stable path, since the units point at it.

## Browse the backups in a browser

`rclone serve http` gives a **read-only** web listing of any remote or path —
browse and download, no delete. To see **local + cloud in one view**, make a
`combine` remote that merges the local dest with the bucket, then serve it:

```bash
nix run .#rclone -- config create both combine \
    upstreams "local=/media/.../ingest remote=b2:my-bucket/ingest"
nix run .#rclone -- serve http both: --addr :8080     # http://<box>:8080
```

Add `--user U --pass P` for basic auth; bind to your LAN, not the public
internet. For an admin (read-write) UI instead — transfers, deletes — use the
rclone Web GUI: `nix run .#rclone -- rcd --rc-web-gui` (fetches the GUI bundle
once, needs internet).

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
