#!/usr/bin/env python3
"""Send an image to the Waveshare RP2350-LCD-1.47 over USB serial.

Loads any image Pillow can read, fits it to the 172x320 panel, encodes it as
big-endian RGB565, and streams it framed to the board's USB CDC serial port.

Wire format (must match firmware/main.c):

    magic   "WSI1"        4 bytes
    format  0x00 (RGB565) 1 byte
    flags   0x00          1 byte
    width   uint16 LE     2 bytes   (1..172)
    height  uint16 LE     2 bytes   (1..320)
    resv    0x0000        2 bytes
    pixels  W*H*2 bytes, row-major, top-left origin, high byte first.

Usage:
    send_image.py IMAGE [--port /dev/ttyACM0] [--fit letterbox|stretch|crop]
                        [--rotate 0|90|180|270] [--list]
"""
import argparse
import struct
import sys
import time

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("error: Pillow is required (nix run .#send handles this for you)")

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("error: pyserial is required (nix run .#send handles this for you)")

PANEL_W = 172
PANEL_H = 320
try:
    RESAMPLE = Image.Resampling.LANCZOS  # Pillow >= 9.1
except AttributeError:  # pragma: no cover - older Pillow
    RESAMPLE = Image.LANCZOS
MAGIC = b"WSI1"
FMT_RGB565_BE = 0x00
RPI_VID = 0x2E8A  # Raspberry Pi (RP2040/RP2350) USB vendor id


def find_port() -> str:
    """Auto-detect the board's serial port by USB vendor id, else first ACM."""
    ports = list(list_ports.comports())
    for p in ports:
        if p.vid == RPI_VID:
            return p.device
    for p in ports:
        if "ACM" in p.device or "usbmodem" in p.device:
            return p.device
    raise SystemExit(
        "error: could not auto-detect the board's serial port; pass --port. "
        "Available: " + (", ".join(p.device for p in ports) or "none")
    )


def to_rgb565_be(img: Image.Image) -> bytes:
    """Convert an RGB image to big-endian RGB565 bytes (high byte first)."""
    img = img.convert("RGB")
    out = bytearray(img.width * img.height * 2)
    i = 0
    for r, g, b in img.getdata():
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[i] = (v >> 8) & 0xFF
        out[i + 1] = v & 0xFF
        i += 2
    return bytes(out)


def fit_image(img: Image.Image, mode: str) -> Image.Image:
    """Resize `img` to exactly PANEL_W x PANEL_H using the chosen strategy."""
    target = (PANEL_W, PANEL_H)
    if mode == "stretch":
        return img.resize(target)
    if mode == "crop":
        # Scale to cover, then centre-crop (fills the panel, may lose edges).
        return ImageOps.fit(img, target, method=RESAMPLE)
    # letterbox (default): scale to fit inside, pad with black.
    fitted = ImageOps.contain(img, target, method=RESAMPLE)
    canvas = Image.new("RGB", target, (0, 0, 0))
    canvas.paste(fitted, ((PANEL_W - fitted.width) // 2,
                          (PANEL_H - fitted.height) // 2))
    return canvas


def build_frame(pixels: bytes, w: int, h: int) -> bytes:
    header = MAGIC + struct.pack("<BBHHH", FMT_RGB565_BE, 0, w, h, 0)
    return header + pixels


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", nargs="?", help="path to an image file")
    ap.add_argument("--port", help="serial device (default: auto-detect)")
    ap.add_argument("--fit", choices=["letterbox", "stretch", "crop"],
                    default="letterbox", help="how to fit the image to 172x320")
    ap.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], default=0,
                    help="rotate the image before sending")
    ap.add_argument("--baud", type=int, default=115200,
                    help="baud rate (ignored by USB CDC, but some stacks want it)")
    ap.add_argument("--list", action="store_true",
                    help="list candidate serial ports and exit")
    args = ap.parse_args()

    if args.list:
        for p in list_ports.comports():
            vid = f"{p.vid:04x}" if p.vid else "----"
            print(f"{p.device}\tVID={vid}\t{p.description}")
        return 0

    if not args.image:
        ap.error("IMAGE is required (or use --list)")

    img = Image.open(args.image)
    if args.rotate:
        img = img.rotate(-args.rotate, expand=True)  # clockwise
    img = fit_image(img, args.fit)
    pixels = to_rgb565_be(img)
    frame = build_frame(pixels, img.width, img.height)

    port = args.port or find_port()
    print(f"sending {img.width}x{img.height} ({len(frame)} bytes) to {port}")

    with serial.Serial(port, args.baud, timeout=3) as ser:
        ser.write(frame)
        ser.flush()
        # Firmware replies "OK <w> <h>" (or "ERR ...") once the frame is drawn.
        deadline = time.time() + 3
        while time.time() < deadline:
            line = ser.readline().decode("ascii", "replace").strip()
            if line:
                print(f"board: {line}")
                return 0 if line.startswith("OK") else 1
    print("warning: no acknowledgement from board (image may still have drawn)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
