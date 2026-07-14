"""Shared USB-serial wire protocol for the RP2350-LCD-1.47 firmware.

Frame layout (must match firmware/main.c):

    magic   "WSI1"        4 bytes
    format  0x00 (RGB565) 1 byte
    flags   0x00          1 byte
    width   uint16 LE     2 bytes
    height  uint16 LE     2 bytes
    resv    0x0000        2 bytes
    pixels  W*H*2 bytes, row-major, top-left, big-endian RGB565.
"""
import struct

from serial.tools import list_ports

MAGIC = b"WSI1"
FMT_RGB565_BE = 0x00
RPI_VID = 0x2E8A  # Raspberry Pi (RP2040/RP2350) USB vendor id


def to_rgb565_be(img) -> bytes:
    """Convert a PIL image to big-endian RGB565 bytes (high byte first)."""
    img = img.convert("RGB")
    out = bytearray(img.width * img.height * 2)
    i = 0
    for r, g, b in img.getdata():
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[i] = (v >> 8) & 0xFF
        out[i + 1] = v & 0xFF
        i += 2
    return bytes(out)


def build_frame(pixels: bytes, w: int, h: int) -> bytes:
    return MAGIC + struct.pack("<BBHHH", FMT_RGB565_BE, 0, w, h, 0) + pixels


def encode_image(img) -> bytes:
    """A full ready-to-send frame for a PIL image."""
    return build_frame(to_rgb565_be(img), img.width, img.height)


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
        "error: could not auto-detect the board's serial port; set the port "
        "explicitly. Available: " + (", ".join(p.device for p in ports) or "none")
    )


def read_ack(ser, timeout: float = 3.0):
    """Read one 'OK ...'/'ERR ...' acknowledgement line, or None on timeout."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = ser.readline().decode("ascii", "replace").strip()
        if line:
            return line
    return None
