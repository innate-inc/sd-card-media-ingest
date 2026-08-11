# Architecture

The SD-card ingest station copies footage off a bank of USB card readers,
verifies it, uploads it to the cloud, and shows live status on a web page. All
policy lives in the host daemon; the risky copy/verify/upload is delegated to
**rclone**, and the display is a page the daemon serves itself.

```
  readers (USB hub)          host                       any browser
  ┌───────────────┐  discover + rclone copy/verify   ┌───────────────────┐
  │ reader 1: SD  │────────────┐                     │ GET /   status    │
  │           µSD │            │  static HTML + CSS  │ 4-stage bars,     │
  │ reader 2: ... │─► ingest ──┴───── :8081 ────────►│ meta-refresh, no  │
  │ ...           │   daemon                         │ JavaScript        │
  └───────────────┘      │  ▲                        └───────────────────┘
             dest_base/  │  └── POST / (col + uuid) ◄────────┘
             <uuid>/     ▼
             <date>/  ┌──────────┐   rclone
                      │ uploader │──────────► cloud (S3 / B2 / Drive)
                      └──────────┘
```

The ingest daemon and the uploader are **decoupled**: the daemon copies +
verifies to local disk and wipes on confirm; the separate uploader pushes to the
cloud on its own schedule — streaming the already-complete files *during* the
copy (skipping rclone's `*.partial` temps) and finalising once verified (a card
can be wiped and gone while its local copy is still uploading). They coordinate
through small per-dir files: `metadata.json` (the copier's receipt, written when
verified) and `uploaded.json` (the uploader's done marker), one writer each; a
`<dir>.copying` marker tells the uploader the copier is still writing, and a
`<dir>.uploading` sibling carries the live uploaded-byte count for the display.

## Components

Status: **built** = exists and runs; **legacy** = an earlier generation still in
the tree, to be replaced; **planned** = designed, not yet code.

| Component | Where | Status | In short |
|-----------|-------|--------|----------|
| **Host ingest daemon** | `host/ingest*.py` | built | Discovers readers in physical order (`/dev/disk/by-path`), auto-mounts each card it finds unmounted (read-write under `/run/ingest/`, unmounted after a real wipe and on removal — a headless box has no desktop mounter; an already-mounted card is left as-is), runs the copier, and publishes a frame for the web display. Split into small modules — `ingest_config`, `ingest_discovery`, `ingest_copier` (**the only file that deletes**), `ingest_view`, `ingest_web`, `ingest_link`, thin `ingest.py`. `--dry-run` runs the full lifecycle over fake cards: `nix run .#ingest -- --dry-run`, then open the page. |
| **Copier** | `host/ingest_copier.py` | built | Per card: `scan → rclone copy → rclone check → SHA1SUMS receipt + metadata.json → pending → guarded wipe`. rclone owns the whole-dir copy + independent-double-read verify; the wipe stays ours (confirm-gated, dry-run by default, per-file size+mtime guard). |
| **Web display** | `host/ingest_web.py`, `host/ingest_view.py` | built | One endpoint of static HTML + CSS served by the daemon (`[web] addr`). `ingest_view` does the segment maths; `ingest_web` renders it and handles the confirm POST, which is the only authorisation to wipe. |
| **Uploader** | `host/uploader.py` | built | Separate process (`nix run .#uploader`): streams each ingest to the cloud with rclone *as it's copied* (completed files only, `*.partial` excluded), then once verified checks against the remote's own metadata hashes (no download) and writes `uploaded.json`. Decoupled from the daemon. Also mirrors the plain directories listed in `[backup] paths` on a much slower clock (copy + check, nothing written back into them). |
| **systemd** | `deploy/` | built | `ingest.service` + `uploader.service`, installed by `nix run .#install-service` (bakes binary paths + the project dir as `WorkingDirectory`, so they read `./ingest.toml` and `./rclone.conf`). |
| **Tests** | `tests/` | built | `nix flake check`: `ingest-unit` (copier, uploader, view and web over a fake card tree, real rclone, real HTTP) and `station-render` (the real daemon in `--dry-run` serving the real page, asserted to render a card bar). |

## The web display

One endpoint, served by the ingest daemon itself (`[web] addr`, default
`:8081`). Static HTML and CSS: no JavaScript, no build step, no client-side
state. `host/ingest_view.py` turns jobs into render-ready numbers,
`host/ingest_web.py` turns those into a page.

| Route | Meaning |
|-------|---------|
| `GET /` | The station: one card per occupied slot, each with a four-stage bar, capacity, rate and ETA. Reloads itself every 2 s via `<meta http-equiv="refresh">`. |
| `POST /` with `col` + `uuid` | Confirm a wipe of that slot. 303 back to `GET /` on success (so a browser refresh cannot re-submit), 409 if refused. |

Anything else 404s. Responses are `no-store`: a cached page is a stale page,
and stale pages are exactly what the confirm check below defends against.

Bars use a **relative scale** — each column is that card's own capacity, so the
four stages fill it and the leftover is that card's free space. Permilles come
from cumulative boundaries that are rounded and then differenced, so the stack
always sums to exactly the used fraction instead of jittering by a permille each
tick as the copied/uncopied split moves.

### The confirm is the wipe interlock

Deletion is never automatic. A confirm is the only authorisation, and the page
renders the button only for a **pending** card (copied and verified).

Moving that button from a physical panel to a web page changes its character in
one way that matters. A button confirms whatever is physically in the slot at
the moment it is pressed. A page confirms what it *displayed*, which may be old
enough for the operator to have pulled the finished card and pushed a fresh one
into the same slot — a card whose data is nowhere else yet.

So the form carries the card's **uuid** as well as its column, and the server
refuses unless that uuid is still in that column *and* still pending. A stale
page cannot wipe the card that replaced the one it was showing. Refusals log at
WARNING: one means someone nearly erased the wrong card.

What this does **not** defend against is reach. Anyone who can open `[web] addr`
can confirm a wipe, where a physical button required standing at the machine.
The uuid check guards mistakes, not attackers — keep the endpoint off the public
internet, and see `[web]` in `ingest.toml` for narrowing the bind address.

`confirm <i>` on **stdin** remains a second confirm channel, for `--dry-run`
and tests. Under systemd stdin is `/dev/null`, so that reader retires at EOF and
HTTP is the only live path.

## Configuration & behaviour of each element

### Web display (`[web]` in `ingest.toml`)
- `addr` — where to serve. `":8081"` is all interfaces; `"127.0.0.1:8081"` or a
  Tailscale address narrows it; `""` disables the server entirely, leaving
  stdin as the only confirm channel.
- Colours and the `numbers` toggle come from `[segments]` — the same block that
  used to drive the panel, unchanged.
- Distinct from `[http] addr`, which is rclone's read-only backup browser.
- There is no separate simulator: `nix run .#ingest -- --dry-run` runs the real
  pipeline over fake cards and serves the real page.

### Host config (`ingest.toml`, in the project dir)
The host config decides *everything the device doesn't*:
- **Serial**: the device is found by USB **VID/PID** (`[serial]`), or pipe mode.
- **Hub selection**: which USB hub's readers to watch (ignore system disks).
- **Destination**: `dest_base`; files land in `dest_base/<label>-<uuid>/<ingest_date>/`.
- **Hashing**: `[hash] algo` — **sha1** by default (the hash Drive/B2/S3 all
  serve from metadata, so the remote is verifiable without downloading).
- **Segments**: colours for the four pipeline stages + the empty colour, plus
  `numbers`. Okabe-Ito (colourblind-safe) by default.
- **Remote**: `[remote] base` — the rclone destination for the uploader (empty
  = no uploading). Credentials live in rclone's own config.
- **Backup paths**: `[backup] paths` — plain directories mirrored to the
  same remote beside the card pipeline, on their own `interval`. Nothing
  is written back into them.
- **Wipe**: `[wipe] enabled = true` arms real deletion (the daemon logs
  `wipe ARMED` at startup); otherwise a confirm only logs what it would delete.

### Plain-path backups (`[backup]`)
The uploader runs a second, much slower sweep beside the card pipeline: every
`[backup] interval` seconds (6h by default) it mirrors each `{src, dst}` entry
in `[backup] paths` to `<remote base>/<dst>` with `rclone copy`, then verifies
it with `rclone check --one-way` against the remote's own hashes.

It is deliberately **not** the card path. A card ingest is a state machine with
a display segment and a wipe interlock behind it, so it earns `uploaded.json`
and a `REMOTE_SHA1SUMS` proof written *into* the ingest dir. A backup path has
none of those, and the directories belong to another application (Immich
indexes its library; the Immich container writes its own DB dumps), so the
sweep writes **nothing** into `src` — `rclone copy` is idempotent, and the
remote is the state.

The two halves run on separate clocks. `interval` (default 5 min) drives the
copy pass, which is a listing diff and costs almost nothing, so new data reaches
the remote within minutes. `verify_every` (default 24h) drives a full hash
scrub, because `rclone check` re-reads every byte of `src` -- ~2.6s for an 11G
library off NVMe, roughly 60s off a spinning disk, and it grows with the
library. A copy that actually moved bytes is checked immediately regardless, so
newly-arrived data is never left unverified.

The sweep runs on its **own daemon thread**, never inside the card loop: a
first full mirror can take hours, and the card sweep is latency-critical
because the display's green segment gates a destructive wipe. Anything the
sweep raises is caught there, for the same reason -- a bad backup path must
never cost a card its upload.

Two invariants:

- **`copy`, never `sync`.** The remote holds cards that were wiped locally long
  ago, so it is a *superset* of any local tree. A sync would delete that
  history.
- **A missing `src` fails closed** — logged loudly and skipped, never read as
  "the source is empty". That is the unmounted-volume guard; together with copy
  semantics, a vanished disk cannot propagate as a deletion.

A plain folder sitting inside `dest_base` is ignored by the card sweep (it has
no `metadata.json` and no `.copying` marker), so the two can share a tree.

### Copier state machine (per card)
`idle → copying → verifying → pending (verified, SHA1SUMS + metadata.json
written) → [confirm] → wiping → empty`. Copy + verify are `rclone copy` then
`rclone check`; deletion happens **only** at the end, after every file is
verified and a human confirms, and is a dry-run unless armed. The separate
uploader later writes `uploaded.json` (it never touches `metadata.json`).

### The four display stages
The bar climbs through **uncopied → copied → verified → uploaded** (orange →
yellow → blue → green, Okabe-Ito). `uploaded` is driven by the uploader's live
byte count (`<dir>.uploading`, then the final `uploaded.json`), so the green
segment fills progressively as the upload streams — visible while the card is
still present.
