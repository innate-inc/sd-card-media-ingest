# SD-card ingest station — plan

Status: **implemented** (first pass) — but the copier design below has since
been superseded by **rclone**: copy + verify are `rclone copy` / `rclone check`,
the hash is **sha1**, files land in `dest_base/<uuid>/<ingest_date>/`, and a
**separate uploader** (`host/uploader.py`) pushes verified dirs to the cloud.
The daemon lives in `host/ingest*.py`; `--dry-run` runs the whole lifecycle over
fake cards. Wiping defaults to a guarded dry-run (needs `[wipe] enabled` + env
`INGEST_ENABLE_WIPE=1`). See `ARCHITECTURE.md` for the current design; this file
is kept for the original rationale. Untested against real readers (dev box has
none).

## Goal & scope

Copy footage off a bank of USB SD/µSD readers, verify every file by hash, and
only then (with a human's OK) wipe the cards — showing live per-card status on
the LCD.

**Scope = the reader hub only.** Ignore the machine's own disks (NVMe), the
SanDisk Extreme Pro SSD (that's an ingest *target*, not a source), and anything
not behind the target hub.

## Hardware (as found on `innate52`)

Behind one Terminus USB-2 hub tree (root-port 2) sit **4 Genesys Logic readers**
(`05e3:0749`), each a **dual-slot** unit that enumerates as **2 SCSI LUNs**
(one SD, one µSD). Ordered by USB path:

| USB path | LUN0 | LUN1 | State when surveyed |
|----------|------|------|---------------------|
| `…-0:2.1.1` | sdd | sde | empty |
| `…-0:2.1.3` | sdh | sdi | empty |
| `…-0:2.2`   | sdb | sdc | both cards (238 GB) |
| `…-0:2.4`   | sdf | sdg | both cards (238 GB) |

So **8 physical slots, up to 8 cards.** `/dev/sdX` letters are *not* stable —
use `/dev/disk/by-path` (USB topology) as the stable identity + ordering key.

## Discovery

1. Enumerate block devices whose `ID_PATH` is under the target hub's USB path
   prefix (configurable; defaults to the Terminus hub found above).
2. **Order** by USB path string → stable physical (spatial) order. Optionally an
   operator port→label map (`2.2/LUN0 → "1 SD"`) since path order ≠ the physical
   left-to-right arrangement the operator sees.
3. Within a reader, LUN0 vs LUN1 = the two slots; the LUN→(SD/µSD) mapping is
   fixed per reader model — establish it once and record it.
4. A slot is "present" when its device reports non-zero size (media inserted).
   Show present slots (config can also show empty slots greyed out).

## Copier — per-card state machine

```
idle ─insert─► copying ─► verifying ─► pending ─confirm─► wiping ─► empty
                  │            │  (all files copied &        │
                  └── error ◄──┘   hash-verified;            └─ error
                                   manifest written)
```

Rules (locked decisions):

- **Whole-file copy** into `dest_base/<partition-UUID>/…` (`dest_base` from
  config; UUID from the card's partition).
- **Hash-verify** every file (source vs destination).
- **Manifest at the very end**: once *all* files are copied and *all* hashes
  match, write a manifest (files + hashes + UUID + timestamp) into the dest dir.
  The manifest is the record of a verified ingest.
- **Never auto-delete.** After the manifest, the card enters **`pending`** and
  waits for explicit **human confirmation**. Deletion is **all-or-nothing at the
  end** — only after everything is verified and confirmed does the card get
  wiped.
- Cards are read-only until the wipe step.

## Human confirmation (mechanism — open)

The 1.47" panel is **not** touch (the touch model is the separate
RP2350-Touch-LCD-1.46). Options, cheapest first:

1. **BOOTSEL button over serial** — the RP2350 can read its BOOTSEL button at
   runtime; firmware sends a `confirm` event back over the serial link. Zero
   extra hardware, but one button = coarse (confirm the highlighted/all pending).
2. **USB button / macropad / footswitch** on the host — per-slot confirm.
3. **CLI / web confirm** — a command or small web page on the network.
4. **Upgrade to the touch panel** for real per-card touch.

Recommendation: start with (3) CLI for correctness, add (1) as the ergonomic
default. Whatever the mechanism, it produces a "confirm slot N" event the daemon
consumes.

## Display integration

The daemon maps each present card to a display slot (physical order) and, each
tick, computes the four segments from bytes and emits the line protocol
(`ARCHITECTURE.md`):

- **uploaded** — copied *and* verified/manifested,
- **copied** — copied, not yet verified,
- **uncopied** — still only on the card,
- **empty** — free space on the card (the `bg` colour).

Colours, and whether to show numbers, come from the host config. `status`
carries `active`/`pending`/`error`. ETA/size come from the transfer rate and
card size.

## Host config (TOML) surface

```
[serial]   port = "/dev/ttyACM0"
[hub]      path_prefix = "…-0:2"      # which hub's readers to watch
[dest]     base = "/mnt/ingest"       # files -> base/<uuid>/
[hash]     algo = "sha256"
[segments] uploaded="#009E73" copied="#0072B2" uncopied="#E69F00" empty="#202020"
           numbers = true
[confirm]  method = "cli"             # cli | button | serial
[poll]     interval_ms = 500
```

## Failure handling

- **Hash mismatch** → slot `error`, keep the card, never delete; re-copy the bad
  file.
- **Card pulled mid-copy** → abort that slot cleanly, mark error/removed.
- **Duplicate partition UUID** across cards → append a suffix / sub-dir so cards
  don't collide in `dest_base`.
- **Destination full** → pause, surface `error`, don't delete anything.
- **Resume**: an existing manifest for a UUID lets a re-inserted card skip
  already-verified files.

## Cross-machine note

Readers live on `innate52`; the LCD is currently on a laptop. Either run the
display feed over SSH (`daemon → ssh → ingest-display's serial`) or move the
panel to `innate52`. The line protocol is plain text, so piping it anywhere is
trivial.

## Implementation phases

1. Discovery library (enumerate + order + SD/µSD map) — no copying.
2. Copier (copy → verify → manifest), dry-run wipe.
3. Line-protocol emitter → drive the simulator, then the device.
4. Confirmation channel + real wipe.
5. LVGL device firmware port (replaces the WSI1 image firmware).
