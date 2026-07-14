# Design decisions

This project builds firmware for a **Waveshare RP2350-LCD-1.47-A** that displays
whatever image is transmitted to it over USB serial, plus a reproducible Nix
flake that builds, flashes, and drives it. This document records the decisions
made along the way and why.

## 1. Identifying the hardware

The Amazon listing (ASIN `B0F6V5XYTM`) was opened in Chrome and its spec table
read directly. It is the **Waveshare RP2350-LCD-1.47-A**:

| Property        | Value |
|-----------------|-------|
| MCU             | RP2350A (dual Cortex-M33 + dual Hazard3 RISC-V), 150 MHz |
| RAM / Flash     | 520 KB SRAM / 16 MB flash |
| Display chip    | **ST7789V3**, SPI |
| Resolution      | **172(H) × 320(V)**, 262K colour, IPS |
| USB             | USB-C, USB 1.1 device+host, drag-and-drop UF2 flashing |
| Other           | TF-card slot, WS2812 RGB LED |

The exact GPIO wiring and LCD init sequence are **not** on the wiki page; they
live in Waveshare's demo code (`RP2350-LCD-1.47.zip`), which was downloaded and
read. The authoritative pin map (`lib/Config/DEV_Config.h`) is:

| Signal | GPIO | Notes |
|--------|------|-------|
| SPI    | `spi0` | 30 MHz |
| CLK    | GP18 | `GPIO_FUNC_SPI` |
| MOSI/DIN | GP19 | `GPIO_FUNC_SPI` |
| CS     | GP17 | software-toggled |
| DC     | GP16 | |
| RST    | GP20 | |
| BL     | GP21 | PWM backlight |

## 2. Firmware: C + Pico SDK (not MicroPython)

The board is a standard RP2350 with the Raspberry Pi UF2 bootloader, so the
Pico SDK in C is the native, best-supported path and produces a single
drag-and-drop `.uf2`. MicroPython was rejected: pushing ~110 KB of binary
framebuffer per frame over a REPL is awkward, and "firmware" + "reproducible Nix
build" point squarely at a compiled artifact.

The board is built as `PICO_BOARD=pico2` (the upstream RP2350 board). The LCD
pins are configured explicitly, so the exact board variant doesn't matter.

## 3. Reusing Waveshare's LCD driver

The ST7789V3 on this panel needs a specific init sequence **and** a non-obvious
memory offset: because the 172-wide glass sits on a 240-wide controller, the
address window is shifted by `0x22` (34) on whichever axis maps to the short
edge. Getting this wrong yields a shifted or blank image.

Rather than re-derive it, `lib/Config/DEV_Config.*` and `lib/LCD/LCD_1in47.*`
are **vendored unmodified from Waveshare's demo** (MIT-licensed — the permission
grant is in each file header). This is the proven reference for this exact
panel. Only `main.c` is original.

`LCD_1IN47_SetWindows()` is already non-`static` in that driver, so `main.c`
reuses it (window + offset handling) and then streams pixel bytes straight to
SPI, bypassing the demo's per-pixel byte-swap.

## 4. Orientation

Driven in `VERTICAL` (portrait): **172 wide × 320 tall**, matching the panel's
advertised native orientation. `LCD_1IN47.WIDTH = 172`, `HEIGHT = 320`.

## 5. Wire protocol — framed RGB565

A small binary header followed by raw pixels was chosen over a raw dump so the
firmware can validate input and resynchronise after a partial/garbled transfer.

```
offset size field
0      4    magic   "WSI1"
4      1    format  0x00 = RGB565
5      1    flags   0x00 (reserved)
6      2    width   uint16 LE   (1..172)
8      2    height  uint16 LE   (1..320)
10     2    reserved 0x0000
12     W*H*2 pixels, row-major, top-left, 2 bytes/px, HIGH byte first
```

Decisions:

- **Header is little-endian** (natural for the host and the RP2350, both LE).
- **Pixels are big-endian RGB565** (high byte first) — this is the byte order
  the ST7789 expects over SPI in 16-bit mode, so the firmware streams the
  received bytes to the panel with **zero per-pixel work**. The host does the
  (cheap) conversion once.
- **Resynchronisation**: the firmware hunts for the 4-byte magic with a sliding
  window, so noise or an aborted frame can never permanently wedge the parser.
- **Per-byte timeout** (2 s): a stalled transfer is abandoned and the parser
  returns to hunting for magic, rather than blocking forever.
- **Acknowledgement**: after drawing, the firmware writes `OK <w> <h>` (or
  `ERR <reason>`) back on the same port, so the host knows the frame landed.
- **Variable size**: any `w×h` up to the panel is accepted and drawn at the
  top-left, so smaller images work without host-side padding — but the host
  tool defaults to sending a full 172×320 frame.

A full frame is 172×320×2 = **110,080 bytes**, held in a single static buffer
(out of 520 KB SRAM). Over USB 1.1 full-speed (~1 MB/s) that's well under a
second.

## 6. USB serial via `stdio_usb`

The firmware uses the SDK's `pico_enable_stdio_usb`, which exposes a USB CDC ACM
port (`/dev/ttyACM*`). Input is byte-transparent (no CR/LF translation on RX),
so binary framebuffers pass through unharmed; only the human-readable ack on TX
gets CRLF. This is simpler and less error-prone than hand-rolling a TinyUSB CDC
interface, and it's exactly what Waveshare's own demo uses.

## 7. Boot splash

On boot the firmware paints three vertical R/G/B bars before any host contact,
so a powered board visibly proves the panel, SPI wiring, and offsets are correct
even with nothing connected.

## 8. Reproducible Nix flake

`flake.nix` pins **nixpkgs `nixos-24.11`**, chosen because it is the first
stable channel carrying **pico-sdk 2.x / picotool 2.x** (the releases that added
RP2350 support) while still evaluating on older Nix versions.

- `pico-sdk` is overridden with `withSubmodules = true` because USB stdio needs
  the vendored **tinyusb** submodule.
- `PICO_SDK_PATH` is passed to CMake via the environment; the build is fully
  offline (no `FetchContent`).
- `find_package(picotool)` is pointed at the Nix-provided `picotool`
  (`-Dpicotool_DIR=…`) so the SDK never tries to git-clone/build it mid-build.

Outputs:

| Command | Effect |
|---------|--------|
| `nix build .#firmware` | builds `result/firmware.uf2` (+ `.elf`) |
| `nix run .#flash` | `picotool load -f -x` the uf2 onto the board |
| `nix run .#send -- IMG` | convert + stream an image to the panel |
| `nix develop` | dev shell with cmake, arm-none-eabi gcc, picotool, python |

## 9. Flashing

`nix run .#flash` uses `picotool load -f -x`. `-f` reboots a running board into
BOOTSEL via the SDK's reset interface; if that isn't available, hold the **BOOT**
button while plugging in, then run it. The `.uf2` can equally be drag-dropped
onto the `RP2350` mass-storage drive that appears in BOOTSEL mode.

## 10. Host sender (`host/send_image.py`)

Pillow + pyserial. Loads any image, **letterboxes** it to 172×320 by default
(alternatives: `stretch`, `crop`), converts to big-endian RGB565, frames it, and
writes it to the port. The port is **auto-detected** by USB vendor id
(`0x2E8A`, Raspberry Pi) with an ACM fallback, overridable with `--port`.

Pinning to `nixos-24.11` (rather than `nixpkgs-unstable`) has a second benefit:
current unstable requires Nix ≥ 2.18 just to evaluate, whereas 24.11 still
evaluates on older Nix — so the flake works across a wider range of installs.
