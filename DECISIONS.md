# Decision log (chronological)

Newest at the bottom. Topical rationale lives in `DESIGN.md`; this file is the
running order-of-events of *why things changed*.

## Firmware & display foundation
1. **Identified the board** via the Amazon listing (Chrome) + Waveshare wiki:
   Waveshare **RP2350-LCD-1.47-A** — RP2350A, 1.47" **172×320 ST7789V3** SPI LCD,
   USB-C, UF2 flashing.
2. **Firmware in C on the Pico SDK** (not MicroPython): native, produces a UF2,
   fits the "reproducible Nix build" goal.
3. **Vendored Waveshare's MIT LCD driver** (`DEV_Config.*`, `LCD_1in47.*`) rather
   than re-derive the ST7789 init + the tricky 172×320 memory offset (`0x22`).
   Pins: SPI0, CLK18/MOSI19/CS17/DC16/RST20/BL21.
4. **First firmware = "dumb image holder"**: receives framed **RGB565** images
   over USB-CDC serial and blits them. Header `WSI1` + LE dims + big-endian
   pixels (the byte order the ST7789 wants, so zero per-pixel work on-device).
5. Dropped Waveshare's `LCD_1IN47_Clear()` — it puts a 110 KB framebuffer on the
   2 KB stack; we clear from our own static buffer instead.
6. **Nix flake pinned to `nixos-24.11`** — first stable channel with pico-sdk 2.x
   / picotool 2.x (RP2350 support) that still evaluates on older Nix.
7. Build fix: bypass nixpkgs' cmake configure hook (it forces host `gcc`) so the
   Pico SDK's `arm-none-eabi` cross toolchain wins. `nix build .#firmware` → uf2.
8. **Orientation bug**: Waveshare's scan-dir names are inverted — `HORIZONTAL`
   gives the portrait 172×320 frame; `VERTICAL` left the image in the left half.
   Fixed to `HORIZONTAL`. Verified full-screen on hardware.

## Progress-bar UI (the ingest station output layer)
9. **Output layer = 8→N progress bars on the LCD**, driven by **JSON on stdin**
   (unixy: any process can pipe updates). CLI, not a GUI.
10. **Single-instance `flock`** so only one process owns the display; all updates
    funnel through its stdin.
11. **Everything configurable via TOML** (screen, layout, colours, fonts).
12. **Dynamic slots, not hardcoded SD/µSD**: show however many devices are
    plugged in, in **physical (spatial) port order**. Discovery/ordering is the
    *feeder's* job; the display just renders the ordered set it's fed.
13. **Vertical-column gauge design** (brainstorm): each device = a tall column,
    vertical white text (slot# + truncated name), background = used-vs-free
    space gauge.
14. **Colourblind-safe palette** (Okabe-Ito): orange = used, bluish-green = free
    — not green/yellow, which is not CVD-safe.
15. **Label alternates at 0.5 Hz** between "slot# name" and **ETA + size (GB)**.
    ETA derived from a feeder-provided transfer rate. Percent stays visible.
16. **ETA format = single unit**: 1 decimal for hours (`1.5h`), no decimal for
    minutes/seconds (`3m`, `9s`).
17. **Orientation = landscape columns**: panel mounted sideways (320×172 drawing
    area, `screen.rotate = 90`), 4 columns wide. Chosen over portrait.
18. **4 wide is the target; page/cycle through more than 4** rather than shrink.

## Pivot: on-device UI framework
19. **Move the UI onto the device** so it owns scrolling/paging/animation instead
    of being a dumb image holder; host sends compact data, not pixels.
20. **Framework = LVGL (C)**: best RP2350/ST7789 support (Waveshare ships an LVGL
    demo for this board), built-in scrolling/paging/animation widgets, and an
    **SDL desktop simulator** so the same UI code runs in a window for testing.
    Reuses our C + Pico SDK + Nix toolchain. (Alternatives weighed: Slint —
    nicer desktop preview but newer/tighter on RP2350; embedded-graphics — Rust,
    no built-in widgets.)
21. **Heartbeat pixel**: a pixel top-left blinks whenever an update/heartbeat
    arrives from the PC — a liveness indicator.

## Ingest station (copier) — plan-level decisions
22. **Scope = the reader hub only.** On `innate52` there are 4 dual-slot Genesys
    USB readers behind a Terminus hub (each reader = 2 LUNs = SD + µSD), ordered
    by USB path. Ignore the SanDisk SSD / nvme / other drives.
23. **Copier = copy whole files → `<dest_base>/<partition-UUID>/…`** (dest base
    from config), then **hash-verify**.
24. **Never auto-delete.** Delete from the SD only after **human confirmation**
    (mechanism TBD — the 1.47 panel is *not* touch; options include reading the
    board's BOOTSEL button at runtime and sending a confirm event, a USB
    button/macropad, or a CLI/web confirm).
25. **Deletion is all-or-nothing at the very end**: only after *all* files are
    copied and *all* hashes confirmed. At that point **write a manifest** (the
    record of the verified ingest) before any wipe.

## LVGL build & generic gauge
26. **Generic multi-segment gauge**: the firmware renders a stack of up to 4
    `(portion, colour)` segments per slot with no hardcoded meaning; the server
    decides meaning/colours (uploaded / copied / uncopied / empty) and toggles
    numbers. Leftover shows a configurable background ("empty") colour.
27. **Line protocol over JSON** (host→device): newline `slot/count/bg/numbers/hb`
    commands, `sscanf`-parseable with no allocator on the MCU.
28. **Simulator-first**: shared UI in `app/`, an SDL desktop target in `sim/`,
    both built by Nix (`nix run .#sim`). LVGL pinned as a flake input (v9.2.2).
    Build fixes: force `LV_DRAW_SW_ASM_NONE` + strip LVGL's NEON `.S` files (x86
    can't assemble them); `EXCLUDE_FROM_ALL` LVGL's install; bigger LVGL heap
    under `LV_SIM` so snapshots fit. Verified by headless `--shot` PNG.
29. **Landscape columns confirmed on real LVGL**: 320×172, 4 columns/page,
    cycle pages >4, label toggles name↔eta/size at 0.5 Hz, heartbeat pixel.
30. **Docs**: added `ARCHITECTURE.md` (components + protocol + config) and
    `INGEST_PLAN.md`; a Fable subagent reviewed the architecture doc and caught
    over-claims (device LVGL firmware / host feeder / copier are *planned*, not
    built) — corrected to mark built vs legacy vs planned honestly.
31. **Device firmware ported** (`device/`): the shared `app/` UI now runs on the
    RP2350 via LVGL, driving the ST7789 in VERTICAL scan (landscape 320×172, no
    LVGL rotation) with an RGB565 byte-swap in the flush. `nix build
    .#firmware-ui`; built, flashed, and driven over serial. The WSI1 image
    firmware is kept as `flash-image`.
32. **Mock-driven tests** (`tests/`, `nix flake check`): proto unit test (fake
    serial lines → asserted model) + sim-render integration (mock feed → real
    LVGL → non-blank frame, headless).

## Display refinements (relative scale, GB, legend)
33. **Relative scale**: each card's bar = its *own* capacity (not a shared
    max). Purely a feeder concern — it changes how the host computes each
    segment's permille, not the firmware.
34. **Numbers = gigabytes only, no percentages.** Dropped the per-column total
    "%" label and the segment-percent fallback; a segment with unknown size
    just draws no number.
35. **Colour legend page**: a new `legend`/`legend clear` protocol command lets
    the host name each colour; the device renders it as the **leftmost page**
    of the scroll (a colour key), with card pages after it. No legend sent =
    no legend page.
36. **Done-state visual = full-green fill, black text** (agreed). The panel is
    display-only, so instead of touch, **input = the RP2350 BOOTSEL button**.
37. **One-button navigation** (short vs long press ≥ 600 ms): browse (auto
    cycle) → *press* wakes to **select** (white box round a card, short = next
    card, page follows it) → *long* opens **detail** (path + per-segment GB +
    status, red delete zone on the right ¼) → *long* **arms** → *click*
    **confirms**. Three deliberate actions to wipe; 12 s idle falls back to
    browse. **No page dots** — the per-column slot number is the position cue.
38. **Detail data**: new `path <i> <text>` command carries an optional
    UUID/mount string for the detail screen (device still can't invent it).
39. **Confirm channel built** (device → host): on confirm the firmware prints
    `confirm <i>` over the same USB-CDC link; the host treats it as the *only*
    authorisation to wipe. BOOTSEL is sampled at runtime by tri-stating the
    QSPI CS (RAM-resident, IRQs off), per the Pico SDK button example.
40. **Sim drives the button from stdin** (`press short`/`press long`) and prints
    `confirm <i>` to stdout, so the whole gesture flow is testable headlessly.
41. **Interactive sim button** = the **SPACE** key, read as key *state* (not
    events, so it doesn't fight LVGL's SDL event pump) with real hold timing;
    ESC quits. Lets you exercise short/long press in the window.
42. **Mock server** (`host/mock_feed.py`, `nix run .#mock`): animates fake
    cards' copy/upload progress as protocol lines, for driving the sim or the
    board with no real readers.

## Wipe UX, liveness, and lifecycle
43. **Hold-to-wipe = one 5 s hold** (not a quick arm + confirm click). The
    delete zone fills like a progress bar over `ARM_MS` (5 s); at the top it
    fires the wipe directly. Navigation long-press stays snappy (`LONG_MS`
    600 ms). The separate "armed" state was removed.
44. **Liveness = whole-screen, not a pixel.** The blinking heartbeat pixel is
    gone; instead, if the host sends nothing for `STALE_MS` (2 s) a dim
    blackish-red "no signal" scrim drops over the frozen last frame. Requires
    the feeder to emit `hb` at least ~once/second even while busy.
45. **A stale screen is inert.** While "no signal" is showing, the button does
    nothing and any in-progress hold/navigation is aborted back to browse — you
    can't arm a wipe off stale data.
46. **Done card = full green + black label text** (readable on the green bar).
47. **Wiped → empty, pinned in place.** After a wipe: a brief `WIPED` flash,
    then the slot becomes a blank `empty` row **at the same absolute index**
    (never reordered/collapsed — physical position is the identity). An empty
    reader is the same blank row. The mock drives the whole arc on a timer
    (copying → done → WIPED → empty → re-inserted) and models **intermittent
    devices** (e.g. slot 2 sits empty most of the time) so gaps hold position.

## Real host daemon + review-driven hardening
48. **Real ingest daemon built** (`host/ingest.py`, `nix run .#ingest`): stdlib
    discovery (`/dev/disk/by-path`) → copier (`copying→verifying→pending→
    wiping→empty`, whole-file copy, hash-verify, manifest-before-wipe) →
    protocol emitter → `confirm <i>` reader → **triple-guarded** wipe
    (`[wipe] enabled` **and** `--enable-wipe` **and** not `--dry-run`; else a
    logged dry-run). `--dry-run` runs the whole lifecycle over fake cards.
    Tests: `ingest-unit` + `ingest-render` in `nix flake check`.
49. **Mock deleted.** `host/mock_feed.py` is gone; **`ingest.py --dry-run` is
    the canonical sim/board driver** — it models the real lifecycle
    (`pending`/`error`/resume/confirm) the mock structurally couldn't. Supersedes
    item 42. `sim-render` keeps a fixed inline serial feed as a dumb smoke test.
50. **Wipe gated to finished cards only.** The detail/wipe screen opens (and
    `confirm` fires) *only* for a `done`/`pending` slot; an empty slot, a
    still-copying card, or an errored card can't be armed — closing a
    wipe-the-wrong/absent-card hole. Refines item 37.
51. **Dropped the WIPED flash** (revises item 47): a wiped card drops straight to
    the blank `empty` row (a wiped card holds no data, so a full-bar "WIPED"
    flash was misleading). Both the deleted mock and `ingest.py`'s post-wipe
    frame did this; `ingest.py` now emits the plain empty row.
52. **Stale scrim is grey, not red** (revises item 44): red is reserved for real
    errors; a quiet feed is just "no signal", so the scrim is dim grey.
53. **Protocol robustness** (from the xhigh protocol review): commands are
    dispatched on a **CR/LF/space-trimmed** copy of the line, so a trailing
    `\r` from a CRLF host no longer breaks exact-match commands (`clear`,
    `legend clear`) or the sim's `press` scripts; `clear` now also drops all
    per-slot `detail` so a stale path/UUID can't outlive its card; empty-text
    `legend` rows are rejected. Documented the **hb liveness contract** (feeder
    must emit within `STALE_MS`). Deferred (design changes, not yet done):
    per-card identity in `confirm <i>`, and a flexible/negotiated segment count.
54. **Dead code + docs swept.** Removed unused `slot_t.nsegs` and a stray
    `<string.h>` in the device firmware; deduped the field-copy pattern into
    `copy_field()`. Synced `ARCHITECTURE.md`/`INGEST_PLAN.md`/`README.md`: host
    daemon now "built", nav table reflects the single 5 s hold (item 43), the
    heartbeat pixel is gone (item 44).
