# SD-card / USB media ingest station

Copy footage off a bank of USB card readers, hash-verify every file, and only
wipe a card once a human confirms — with live per-card status on a small LCD.

The system splits into a **dumb display** and a **smart host**:

- **Device** (`device/`, `app/`) — a **Waveshare RP2350-LCD-1.47-A** running an
  LVGL UI. It knows nothing about cards or copying; it renders the stacked
  progress bars and text the host sends over a plain-text serial line protocol,
  and sends back `confirm <i>` when the operator approves a wipe (via the
  board's BOOTSEL button).
- **Host** (`host/ingest.py`) — discovers the readers in physical order, copies
  each card, hash-verifies, writes a manifest, and drives the display. It is the
  only thing that ever deletes, and only on an explicit confirm.
- **Simulator** (`sim/`) — the exact device UI in an SDL window, for developing
  without hardware.

See `ARCHITECTURE.md` for the split + line protocol, `INGEST_PLAN.md` for the
copier design, and `DECISIONS.md` for the running rationale.

## Quick start (Nix)

Requires Nix with flakes enabled (`experimental-features = nix-command flakes`).

```bash
# Watch the whole ingest lifecycle with no hardware: the real daemon drives the
# simulator with fake cards (copy -> verify -> pending -> wipe).
nix run .#ingest -- --dry-run | nix run .#sim

# The real thing: discover readers, copy + verify, await confirm, (dry) wipe.
nix run .#ingest -- --config host/ingest.toml

# Build + flash the on-device display firmware (-> ./result/firmware.uf2).
nix build .#firmware-ui
nix run .#flash
```

In the simulator, **SPACE** stands in for the board's BOOTSEL button (hold past
600 ms = long press); **ESC** quits.

## Wipe safety

Deletion never happens automatically. A card is wiped only after every file is
copied *and* hash-verified *and* the operator sends `confirm <i>` — and even
then it defaults to a logged dry run. Real deletion needs **both** `[wipe]
enabled = true` in `host/ingest.toml` **and** `--enable-wipe` on the CLI. See
`host/ingest.toml` for the full config surface.

## Legacy: USB image-display firmware

Before the ingest UI, this board ran a simpler "dumb image holder" firmware
(`firmware/`) that blits any image streamed to it over USB serial. It is kept
for reference and still builds:

```bash
nix build .#firmware                                 # -> ./result/firmware.uf2
nix run .#flash-image                                # flash it
nix run .#send -- path/to/picture.jpg                # stream an image
nix run .#send -- IMG --fit letterbox|stretch|crop   # default: letterbox
nix run .#send -- IMG --rotate 90 --port /dev/ttyACM0
nix run .#send -- --list                             # list serial ports
```

Wire format (`DESIGN.md §5`): a 12-byte little-endian header (`"WSI1"`, format,
flags, `width` u16, `height` u16, reserved) followed by `width*height*2` bytes
of big-endian RGB565 pixels; the board replies `OK <w> <h>` or `ERR <reason>`.

## Board doesn't show up

The tools find the board by its USB id (`2e8a`). If `nix run .#flash` says "no
devices in BOOTSEL" or `nix run .#send -- --list` shows no `2e8a` port:

- Plug the board **directly into the computer**, not through a USB hub.
- Use a **data** USB-C cable, not a charge-only one.
- For flashing, hold the **BOOT** button while plugging in to force BOOTSEL mode
  (it appears as an `RP2350` mass-storage drive; you can also drag the `.uf2`
  onto it).
- Once firmware is running it enumerates as a serial port (`/dev/ttyACM*`).

## Serial permissions

`picotool` may need udev rules or `sudo`; the CDC port (`/dev/ttyACM*`) needs
your user in the `dialout` group (log out/in after `usermod -aG dialout $USER`).
