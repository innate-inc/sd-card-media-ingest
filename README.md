# SD-card / USB media ingest station

Copy footage off a bank of USB card readers, hash-verify every file, upload it
to the cloud, and only wipe a card once a human confirms — with live per-card
status on a web page.

All policy lives in the host daemon; the risky copy/verify/upload is delegated
to **rclone**:

- **Ingest daemon** (`host/ingest*.py`) — discovers readers in physical order,
  and per card runs `rclone copy` → `rclone check` into
  `dest_base/<label>-<uuid>/<ingest_date>/`, then waits for a confirm to wipe. It is the
  only thing that deletes, and only on an explicit confirm.
- **Web display** (`host/ingest_web.py`, `host/ingest_view.py`) — one endpoint
  of static HTML + CSS served by the daemon on `[web] addr` (default `:8081`).
  No JavaScript. It shows the four-stage bar per card and carries the confirm
  button, which is the only authorisation to wipe.
- **Uploader** (`host/uploader.py`) — a *separate* process that streams each
  ingest to a cloud remote (rclone) as the files are copied, then proves it by
  re-checking against the remote's own hashes once the ingest is verified.
  Decoupled, so a card can be wiped and gone while its local copy is still
  uploading. It also mirrors any plain directories listed in `[backup] paths`.

The bar climbs through four colourblind-safe stages: **uncopied → copied →
verified → uploaded**. See `ARCHITECTURE.md` for the full split and
`DECISIONS.md` for the running rationale.

## The display

Open `http://<box>:8081/` while the daemon runs. One page, static HTML and CSS,
reloading itself every 2 s — it works in any browser, including a text one.

A card that is copied and verified shows a **confirm** button; pressing it is
the only thing that authorises a wipe. The form carries the card's UUID as well
as its slot, and the server refuses unless that UUID is *still* in that slot and
*still* pending — so a page left open while cards were swapped cannot wipe the
wrong card. Refusals are logged at WARNING.

**Anyone who can reach that address can confirm a wipe.** The UUID check guards
mistakes, not attackers. Narrow it with `[web] addr` (`"127.0.0.1:8081"`, or a
Tailscale address) and keep it off the public internet.

## Quick start (Nix)

Requires Nix with flakes enabled (`experimental-features = nix-command flakes`).
rclone etc. come with the flake apps — nothing else to install.

### Quickstart: test / develop (no hardware)

```bash
# Watch the whole lifecycle with fake cards (copy -> verify -> pending -> wipe),
# then open http://localhost:8081/ and press confirm:
nix run .#ingest -- --dry-run

# ...or auto-confirm the wipe so it runs hands-free:
nix run .#ingest -- --dry-run --auto-confirm 2

nix flake check          # run the test suite (unit tests + a real page render)
```

**Dev shell** — `nix develop` drops you into a shell with python, rclone and
curl. From there you can run things directly:

```bash
nix develop
python3 tests/test_ingest.py                  # host tests (needs rclone -- it's here)
python3 host/ingest.py --dry-run              # run the daemon from source
```

### Quickstart: install / setup / config (on the box)

```bash
# Config + secrets live in THIS repo dir; run everything from here (the systemd
# units get WorkingDirectory baked in, so keep the repo at a stable path).

# 1. One-time cloud remote for the uploader (skip if you only back up locally):
nix run .#rclone -- config                # make a remote "b2" -> ./rclone.conf

# 2. Put THIS box's settings in ./config.toml — it's gitignored and layered on
#    top of the tracked ingest.toml, so `git pull` never conflicts with your
#    local edits. Only override what differs from the defaults, e.g.:
#      [dest]   base = "/media/.../ingest/"   # where copies land
#      [remote] base = "b2:my-bucket/ingest"  # rclone dest ("" = local only)
#      [hub]    vid/pid = the USB hub your readers plug into (`lsusb`; default
#               is the Terminus 1a40:0101). Every drive on that hub is a source.
#      [wipe]   enabled = true                # once you trust it (default false)
$EDITOR config.toml

# Check discovery: list what's plugged into the hub right now (read-only).
nix run .#slots        # never copies; a diagnostic for the [hub] match. e.g.:
#   #    port     dev   size       card
#   1    2.1.1    sdd   -          (empty)
#   5    2.2      sdb   256.0 GB   EXTREME
#   ...
# The web display numbers cards by insertion order, not by these ports.

# 3. Install + start the services:
nix run .#install-service                  # units point at $PWD/ingest.toml
sudo systemctl enable --now innate-sd-ingester-ingest innate-sd-ingester-uploader innate-sd-ingester-http
journalctl -fu innate-sd-ingester-ingest   # watch it work

# 4. When ready to REALLY delete cards after backup, arm the wipe:
#    set [wipe] enabled = true in ./ingest.toml, then: sudo systemctl restart innate-sd-ingester-ingest
#    (it logs "wipe ARMED" loudly at startup).
```

### Updating to a newer version

The systemd units have the nix store path of the built binary **baked in**, so a
`git pull` alone does not update the running services — you must re-run
`install-service` to rebuild and repoint the units, then restart:

```bash
cd ~/sd-card-media-ingest
git pull
nix run .#install-service        # rebuilds + rewrites the unit ExecStart
sudo systemctl restart innate-sd-ingester-ingest innate-sd-ingester-uploader
journalctl -fu innate-sd-ingester-ingest   # confirm the new version is running
```

`install-service` restarts any unit that was already running, so the new build
takes effect immediately; `daemon-reload` alone would leave the old nix-store
binary executing.

## Wipe safety

Deletion never happens automatically. A card is wiped only after every file is
copied *and* hash-verified *and* the operator sends `confirm <i>` — and even
then it defaults to a logged dry run. Real deletion is armed only by `[wipe]
enabled = true` in `ingest.toml` (the daemon logs `wipe ARMED` loudly at
startup). The wipe also re-checks each source (size+mtime) right before deleting
it, and every action is logged (`journalctl -u innate-sd-ingester-ingest`).

## Cloud upload

The uploader pushes each ingest under `dest_base/<label>-<uuid>/<date>/` to
`[remote] base` (an rclone destination like `b2:bucket/ingest`, `gdrive:ingest`,
or a second disk). It starts **while the copy is still running** — the copier
leaves a `<dir>.copying` marker, and each pass the uploader rclone-copies the
already-complete files (skipping rclone's in-flight `*.partial` temps), so bytes
stream to the cloud as they land and the display's green segment fills live.
Once the ingest is verified (`metadata.json`), it runs `rclone check` against
the remote — which reads the backend's stored **SHA1** from object metadata, so
it confirms the bytes are really up there **without downloading** — records the
proof in `REMOTE_SHA1SUMS`, and writes `uploaded.json` to mark it done (the
copier owns `metadata.json`, the uploader owns `uploaded.json` — one writer
each; `uploaded.json` present always means a fully verified upload). The remote
+ credentials come from rclone's own config (`rclone config`).

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

The **innate-sd-ingester-http** service (installed by `.#install-service`) runs
`rclone serve http` — a **read-only** web listing (browse + download, no delete)
on `[http] addr` (default `:8080`, i.e. `http://<box>:8080`).

By default (`[http] target = ""`) it shows **local + cloud in one view** — it
auto-builds an rclone `combine` of the local `[dest] base` and the cloud
`[remote] base` (as `local/` and `cloud/`), no setup needed. If no remote is
configured it just serves the local dest. Set `[http] target` to a specific path
or remote to override.

Run it by hand with `nix run .#http` (append `-- --user U --pass P` for basic
auth). Keep it on your LAN, not the public internet. For an admin (read-write)
UI — transfers, deletes — use the rclone Web GUI:
`nix run .#rclone -- rcd --rc-web-gui` (fetches the GUI bundle once).

## Permissions

The ingest daemon mounts and unmounts cards itself (a headless box has no
desktop mounter) and deletes files on a confirmed wipe, so it runs as **root**
under systemd — that is why `install-service` uses `sudo`. Nothing else needs
special privileges: the uploader and the two HTTP services are unprivileged.
