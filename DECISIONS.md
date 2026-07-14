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
