# RP2350-LCD-1.47 USB image display

Firmware + reproducible Nix tooling that makes a **Waveshare RP2350-LCD-1.47-A**
show any image you stream to it over USB serial.

- `firmware/` — Pico SDK C firmware (vendored Waveshare LCD driver + original
  `main.c` implementing the USB-serial image protocol).
- `host/send_image.py` — converts any image to the wire format and sends it.
- `flake.nix` — builds the `.uf2`, flashes it, and runs the sender, all pinned.
- `DESIGN.md` — every design decision and why.

## Quick start (Nix)

Requires Nix with flakes enabled (`experimental-features = nix-command flakes`).

```bash
# Build the firmware -> ./result/firmware.uf2
nix build .#firmware

# Flash it (hold BOOT while plugging in if the board isn't already running
# firmware that supports reset)
nix run .#flash

# Show an image (auto-detects the serial port)
nix run .#send -- path/to/picture.jpg
```

On boot the panel shows **red/green/blue vertical bars** — that confirms the
display works before you send anything. `nix run .#send -- IMG` replaces it.

Options for the sender:

```bash
nix run .#send -- IMG --fit letterbox|stretch|crop   # default: letterbox
nix run .#send -- IMG --rotate 90
nix run .#send -- IMG --port /dev/ttyACM0
nix run .#send -- --list                              # list serial ports
```

## Board doesn't show up

The sender/flasher find the board by its USB id (`2e8a`). If `nix run .#flash`
says "no devices in BOOTSEL" or `nix run .#send -- --list` shows no `2e8a` port:

- Plug the board **directly into the computer**, not through a USB hub.
- Use a **data** USB-C cable, not a charge-only one.
- For flashing, hold the **BOOT** button while plugging in to force BOOTSEL
  mode (it then appears as an `RP2350` mass-storage drive; you can also just
  drag `result/firmware.uf2` onto it).
- Once firmware is running it enumerates as a serial port (`/dev/ttyACM*`).

## Serial permissions

`picotool` may need udev rules or `sudo`; the CDC port (`/dev/ttyACM*`) needs
your user in the `dialout` group (log out/in after `usermod -aG dialout $USER`).

## Wire protocol

See `DESIGN.md §5`. In short: a 12-byte little-endian header
(`"WSI1"`, format, flags, `width` u16, `height` u16, reserved) followed by
`width*height*2` bytes of big-endian RGB565 pixels. The board replies
`OK <w> <h>` or `ERR <reason>`.
