# Architecture

The SD-card ingest station copies footage off a bank of USB card readers,
verifies it, and shows live status on a small LCD. The design splits cleanly
into a **dumb generic display** (device) and a **smart server** (host): the
device knows nothing about SD cards, copying, or colours — it just renders the
segments and text it is told to. All policy lives on the host.

```
  readers (USB hub)          host / server                     device
  ┌───────────────┐   discover + copy + verify          ┌──────────────────┐
  │ reader 1: SD  │─────────────┐                        │  RP2350 + LCD    │
  │           µSD │             │  line protocol (serial)│  LVGL UI:        │
  │ reader 2: ... │──► ingest ──┴──────── /dev/ttyACM ──►│  stacked gauges, │
  │ ...           │    daemon        (host → device)     │  paging, toggle, │
  └───────────────┘                                      │  heartbeat pixel │
                                                         └──────────────────┘
                       ▲                                          │
                       └───────── confirm-to-wipe ◄───────────────┘
                                  (device → host, TBD)
```

## Components

Status: **built** = exists and runs; **legacy** = an earlier generation still in
the tree, to be replaced; **planned** = designed, not yet code.

| Component | Where | Status | In short |
|-----------|-------|--------|----------|
| **Shared UI core** | `app/` | built | Portable C: `model` (slots + segments), `proto` (parse the serial line protocol), `ui` (LVGL widgets). Compiled unchanged into **both** the device firmware and the simulator. |
| **Simulator** | `sim/` | built | The `app/` UI in an SDL desktop window; stdin stands in for the serial link. `nix run .#sim`. `--shot` renders headless for tests. |
| **Device firmware (LVGL)** | `device/` | built | Runs the same `app/` UI via LVGL on the RP2350/ST7789 (VERTICAL scan = landscape 320×172, RGB565 byte-swapped in the flush), reading the line protocol over USB-CDC. `nix build .#firmware-ui` → uf2; `nix run .#flash`. The earlier WSI1 image firmware lives on in `firmware/` (`nix run .#flash-image`). |
| **Tests** | `tests/` | built | `nix flake check`: a proto unit test (mock serial lines → asserted model) and a sim-render integration test (mock feed → real LVGL → non-blank frame). |
| **Host feeder / server** | `host/` | **planned** | Will discover readers behind the hub in physical order, run the copier, and emit the line protocol. **Today `host/` holds the superseded display driver** (`ingest_display.py`: JSON-on-stdin → PIL → WSI1 frames — a *different* protocol) plus `send_image.py`/`wire.py`. |
| **Copier** | `host/` | **planned** | Per card: copy → hash-verify → manifest → await human confirmation → wipe. See `INGEST_PLAN.md`. |

The diagram above shows the **target** system; the "ingest daemon", copier, and
LVGL device firmware are planned, not yet implemented.

## Protocol (host → device)

Newline-terminated ASCII lines, one command each. Chosen over JSON so the device
parses with `sscanf` and no allocator. Any recognised (non-empty, non-`#`) line
pulses the heartbeat pixel; blank lines and `#`-comments are ignored entirely.

| Command | Meaning |
|---------|---------|
| `hb` | Heartbeat / keepalive (no state change). |
| `clear` | Remove all slots. |
| `count <n>` | Truncate the slot list to `n`. |
| `bg <rrggbb>` | Background / "empty space" colour. |
| `numbers <0\|1>` | Show per-segment numbers on/off. |
| `slot <i> <size_mb> <eta_s> <status> <p0> <c0> <p1> <c1> <p2> <c2> <p3> <c3> <label…>` | Define/update slot `i`. |

Field meanings:

- `i` — 0-based slot index (`0..31`, `MAX_SLOTS = 32`); slots render left→right
  in index order (= physical port order, decided by the host). A `slot` with
  `i >= count` **auto-extends** the count to `i+1`, so you don't have to send
  `count` first.
- `size_mb` — total size in MB, `-1` = unknown (drives the GB numbers).
- `eta_s` — seconds to completion, `-1` = unknown (drives the ETA text). ETA
  phase shows `done` for status `done`, and falls back to "slot# name" if both
  eta and size are unknown.
- `status` — `idle｜active｜done｜error｜paused｜pending`. Currently only `done`
  changes rendering (ETA text). `pending` = verified, awaiting wipe confirmation
  — carried through the model but not yet visually distinct. (Status-based
  tinting is planned, not implemented.)
- `pN cN` — **all four pairs are required**; use `permille 0` for unused
  segments. `permille` is 0..1000 of the whole bar (clamped); colour is hex
  `rrggbb`. Segments stack from the bottom; the leftover shows the `bg` colour.
  The host assigns each segment's meaning (e.g. uploaded / copied / uncopied).
- `label` — the rest of the line, truncated to 23 chars (`MAX_LABEL = 24`).

Per-segment numbers (when `numbers 1`) draw only on segments ≥ 12 px tall, and
show GB when `size_mb ≥ 0`, otherwise the segment's percentage. The number at
the base of each column is the total filled percentage (sum of segment
permilles).

Example tick for one card, 30% uploaded, 20% copied, 25% still on card:

```
bg 202020
numbers 1
slot 0 238000 900 active 300 22c35e 200 0072b2 250 e69f00 0 0 SANDISK64
```

### Device → host (planned)

A confirm channel for the wipe step: the device signals a human confirmation
(e.g. the RP2350 BOOTSEL button read at runtime → `confirm <i>` line back).
Not yet implemented; see `INGEST_PLAN.md`.

## Configuration & behaviour of each element

### Device firmware
- Fixed behaviour, no config file. Renders whatever the protocol says.
- Landscape 320×172 (panel driven in Waveshare VERTICAL scan; no LVGL rotation).
- Shows up to **4 columns per page**; if more slots arrive it **cycles pages**
  (~4 s). Each column's label **toggles every 2 s** between "slot# name" and
  "ETA + size". Top-left **heartbeat pixel** blinks on each received line.

### Simulator
- Same UI. `ingest-sim` opens a 320×172 SDL window and reads the protocol from
  stdin. `--shot <ms> <file.ppm>` renders headless (for tests).

### Host server (TOML config — planned surface)
The host config decides *everything the device doesn't*:
- **Serial**: which port the device is on.
- **Hub selection**: which USB hub's readers to watch (ignore system disks).
- **Destination**: `dest_base`; files land in `dest_base/<partition-UUID>/`.
- **Hashing**: algorithm; copy is verified before it counts.
- **Segments**: the mapping of meaning → colour (uploaded / copied / uncopied)
  and the empty-space colour, plus `numbers` on/off. The host computes each
  segment's permille from bytes and sends them.
- **Confirmation**: how a human approves a wipe (button / CLI / …).

### Copier state machine (per card)
`idle → copying → verifying → pending (verified, manifest written) →
[human confirm] → wiping → empty`. Deletion happens **only** at the end, after
*all* files are copied and hash-verified and a human confirms. See
`INGEST_PLAN.md` for detail.
