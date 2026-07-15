# Architecture

The SD-card ingest station copies footage off a bank of USB card readers,
verifies it, uploads it to the cloud, and shows live status on a small LCD. The
design splits cleanly into a **dumb generic display** (device) and a **smart
host**: the device knows nothing about SD cards, copying, or colours — it just
renders the segments and text it is told to. All policy lives on the host, and
the risky copy/verify/upload is delegated to **rclone**.

```
  readers (USB hub)          host                          device
  ┌───────────────┐  discover + rclone copy/verify   ┌──────────────────┐
  │ reader 1: SD  │────────────┐                      │  RP2350 + LCD    │
  │           µSD │            │  line protocol       │  LVGL UI:        │
  │ reader 2: ... │─► ingest ──┴──── /dev/ttyACM ────►│  4-stage gauges, │
  │ ...           │   daemon                          │  paging, toggle  │
  └───────────────┘      │  ▲                         └──────────────────┘
             dest_base/  │  └─ confirm <i> (BOOTSEL) ◄──────┘
             <uuid>/     ▼
             <date>/  ┌──────────┐   rclone
                      │ uploader │──────────► cloud (S3 / B2 / Drive)
                      └──────────┘
```

The ingest daemon and the uploader are **decoupled**: the daemon copies +
verifies to local disk and wipes on confirm; the separate uploader pushes those
verified dirs to the cloud on its own schedule (a card can be wiped and gone
while its local copy is still uploading). They coordinate through two per-dir
files, one writer each: `metadata.json` (the copier's receipt) and
`uploaded.json` (the uploader's state).

## Components

Status: **built** = exists and runs; **legacy** = an earlier generation still in
the tree, to be replaced; **planned** = designed, not yet code.

| Component | Where | Status | In short |
|-----------|-------|--------|----------|
| **Shared UI core** | `app/` | built | Portable C: `model` (slots + segments), `proto` (parse the serial line protocol), `ui` (LVGL widgets). Compiled unchanged into **both** the device firmware and the simulator. |
| **Simulator** | `sim/` | built | The `app/` UI in an SDL desktop window; stdin stands in for the serial link and **SPACE** stands in for the board's button (hold = long press, ESC quits). `nix run .#sim`. `--shot` renders headless for tests. |
| **Device firmware (LVGL)** | `device/` | built | Runs the same `app/` UI via LVGL on the RP2350/ST7789 (VERTICAL scan = landscape 320×172, RGB565 byte-swapped in the flush), reading the line protocol over USB-CDC. Reads the **BOOTSEL button** at runtime for on-device navigation and emits `confirm <i>` back to the host. `nix build .#firmware-ui` → uf2; `nix run .#flash`. |
| **Host ingest daemon** | `host/ingest*.py` | built | Discovers readers in physical order (`/dev/disk/by-path`), auto-mounts each card it finds unmounted (read-write under `/run/ingest/`, unmounted after a real wipe and on removal — a headless box has no desktop mounter; an already-mounted card is left as-is), runs the copier, and emits the line protocol. Split into small modules — `ingest_config`, `ingest_discovery`, `ingest_copier` (**the only file that deletes**), `ingest_emit`, `ingest_link`, thin `ingest.py`. `--dry-run` runs the full lifecycle over fake cards: `nix run .#ingest -- --dry-run \| nix run .#sim`. |
| **Copier** | `host/ingest_copier.py` | built | Per card: `scan → rclone copy → rclone check → SHA1SUMS receipt + metadata.json → pending → guarded wipe`. rclone owns the whole-dir copy + independent-double-read verify; the wipe stays ours (confirm-gated, dry-run by default, per-file size+mtime guard). |
| **Uploader** | `host/uploader.py` | built | Separate process (`nix run .#uploader`): pushes verified ingest dirs to the cloud with rclone, then verifies against the remote's own metadata hashes (no download) and writes `uploaded.json`. Decoupled from the daemon. |
| **systemd** | `deploy/` | built | `ingest.service` + `uploader.service`, installed by `nix run .#install-service` (bakes binary paths + the project dir as `WorkingDirectory`, so they read `./ingest.toml` and `./rclone.conf`). |
| **Tests** | `tests/` | built | `nix flake check`: `proto` (serial lines → asserted model), `ingest-unit` (copier + emitter + uploader over a fake card tree, real rclone), `ingest-render` (real daemon `--dry-run` → real LVGL → non-blank frame), `sim-render` (fixed serial feed → non-blank frame). |

## Protocol (host → device)

Newline-terminated ASCII lines, one command each. Chosen over JSON so the device
parses with `sscanf` and no allocator. Any recognised (non-empty, non-`#`) line
counts as liveness; blank lines and `#`-comments are ignored entirely. A stray
trailing `\r` (CRLF host) is tolerated on every command.

**Liveness contract:** the feeder must send *something* (any line, e.g. `hb`)
at least every ~1 s. If the device sees no line for `STALE_MS` (2 s) it drops a
dim grey "no signal" scrim over the last frame and goes inert (the button is
dead) until the feed returns. A busy feeder must keep emitting `hb` even while
hashing so it isn't mistaken for a dead link.

| Command | Meaning |
|---------|---------|
| `hb` | Heartbeat / keepalive (no state change). |
| `clear` | Remove all slots. |
| `count <n>` | Truncate the slot list to `n`. |
| `bg <rrggbb>` | Background / "empty space" colour. |
| `numbers <0\|1>` | Show per-segment numbers on/off. |
| `legend <rrggbb> <text…>` | Append a colour→meaning row to the legend (up to `MAX_LEGEND = 6`). |
| `legend clear` | Empty the legend (removes the legend page). |
| `path <i> <text…>` | Optional per-slot detail (UUID / mount path) shown on the detail screen. |
| `slot <i> <size_mb> <eta_s> <kbps> <status> <p0> <c0> <p1> <c1> <p2> <c2> <p3> <c3> <label…>` | Define/update slot `i`. |

Field meanings:

- `i` — 0-based slot index (`0..31`, `MAX_SLOTS = 32`); slots render left→right
  in index order. The host assigns each plugged-in card the lowest free index
  when it appears and holds it until removal (so the list is the cards present,
  in insertion order, and a card's index — hence its `confirm` number — never
  shifts while it's in). A `slot` with `i >= count` **auto-extends** the count
  to `i+1`, so you don't have to send `count` first.
- `size_mb` — total size in MB, `-1` = unknown (drives the GB numbers).
- `eta_s` — seconds to completion, `-1` = unknown (drives the ETA text). ETA
  phase shows `done` for status `done`, and falls back to "slot# name" if both
  eta and size are unknown.
- `kbps` — copy speed in KB/s, `-1` = unknown. While copying, the ETA phase
  shows "eta speed" (e.g. `5m 42MB/s`); the detail screen lists both. The host
  smooths it (EMA) so it doesn't jump.
- `status` — `idle｜active｜done｜error｜paused｜pending`. Currently only `done`
  changes rendering (ETA text). `pending` = verified, awaiting wipe confirmation
  — carried through the model but not yet visually distinct. (Status-based
  tinting is planned, not implemented.)
- `pN cN` — **all four pairs are required**; use `permille 0` for unused
  segments. `permille` is 0..1000 of the whole bar (clamped); colour is hex
  `rrggbb`. Segments stack from the bottom; the leftover shows the `bg` colour.
  The host assigns each segment's meaning (uncopied / copied / verified / uploaded).
- `label` — the rest of the line, truncated to 23 chars (`MAX_LABEL = 24`).

Per-segment numbers (when `numbers 1`) draw only on segments ≥ 12 px tall and
show **gigabytes** (`size_mb × permille`); a segment with unknown `size_mb`
draws no number. Percentages are not shown. Bars use a **relative scale** —
each column is that card's own capacity, so the segments fill it and the
leftover is the card's free space in `bg` colour. (Relative vs absolute is a
feeder decision; it just changes how the host computes permilles, not the
firmware.)

Example tick for one card, 30% uploaded, 20% copied, 25% still on card:

```
bg 202020
numbers 1
slot 0 238000 900 42000 active 300 22c35e 200 0072b2 250 e69f00 0 0 SANDISK64
```

### Device → host

A confirm channel for the wipe step. The device reads the **RP2350 BOOTSEL
button at runtime** (tri-stating the QSPI CS to sample the pad) and, when the
operator completes the on-screen arm+confirm gesture, emits a single line back
over the same USB-CDC link:

| Line | Meaning |
|------|---------|
| `confirm <i>` | The operator confirmed a wipe of slot `i`. The host may now delete that card. |

The host must treat `confirm` as the *only* authorisation to wipe (deletion is
never automatic). See `INGEST_PLAN.md` for the copier's side.

### On-device navigation (one button)

The panel is display-only, so the single BOOTSEL button drives a three-state
machine; **short** vs **long** press (≥ 600 ms) are the only inputs:

| State | short press | long press |
|-------|-------------|------------|
| **browse** (auto-cycling status) | wake → select first card | wake → select first card |
| **select** (white box around a card) | move to next card (page follows it) | open its **detail** (only for a `done`/`pending` card) |
| **detail** (info + red delete zone) | back to select | *hold 5 s* → **confirm** → `confirm <i>` |

The wipe is a single deliberate **5-second hold** in the detail screen: the red
delete zone fills like a progress bar and fires the wipe at the top (there is no
separate "armed" click). A wipe is only offered for a **finished** card
(`done`/`pending`); an empty slot, a still-copying card, or an errored card
can't open the wipe screen at all. Any 12 s of inactivity falls back to browse,
and a stale feed makes the button inert. There are **no page dots** — the
per-column slot number is the position indicator.

## Configuration & behaviour of each element

### Device firmware
- Fixed behaviour, no config file. Renders whatever the protocol says.
- Landscape 320×172 (panel driven in Waveshare VERTICAL scan; no LVGL rotation).
- Shows up to **4 columns per page**; if more slots arrive it **cycles pages**
  (~4 s). When the host sends a legend, it becomes the **leftmost page** (a
  colour key), with the card pages after it. Each column's label **toggles
  every 2 s** between "slot# name" and "ETA + size". Liveness is whole-screen:
  a dim grey **"no signal" scrim** drops if the feed goes quiet (see the
  liveness contract above) — there is no heartbeat pixel.

### Simulator
- Same UI. `ingest-sim` opens a 320×172 SDL window and reads the protocol from
  stdin. **SPACE** is the one button (held past 600 ms = long press); it also
  accepts scripted `press short` / `press long` lines on stdin and prints
  `confirm <i>` to stdout. `--shot <ms> <file.ppm>` renders headless (for
  tests).

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
- **Wipe**: `[wipe] enabled = true` arms real deletion (the daemon logs
  `wipe ARMED` at startup); otherwise a confirm only logs what it would delete.

### Copier state machine (per card)
`idle → copying → verifying → pending (verified, SHA1SUMS + metadata.json
written) → [confirm] → wiping → empty`. Copy + verify are `rclone copy` then
`rclone check`; deletion happens **only** at the end, after every file is
verified and a human confirms, and is a dry-run unless armed. The separate
uploader later writes `uploaded.json` (it never touches `metadata.json`).

### The four display stages
The bar climbs through **uncopied → copied → verified → uploaded** (orange →
yellow → blue → green, Okabe-Ito). `uploaded` is driven by the uploader via
`uploaded.json`, so it fills for a card still present when its upload completes.
