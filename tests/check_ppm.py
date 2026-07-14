#!/usr/bin/env python3
"""Assert a simulator snapshot is a real, non-blank 320x172 frame.

Used by the sim-render check: proves that mocked serial input flowed through
LVGL to an actually-rendered frame (not a uniform/blank screen). Stdlib only.
"""
import sys

data = open(sys.argv[1], "rb").read()
assert data.startswith(b"P6"), "not a PPM"
hdr_end = data.index(b"255\n") + 4
px = data[hdr_end:]
assert len(px) == 320 * 172 * 3, f"unexpected frame size {len(px)}"
distinct = len(set(px))
assert distinct > 3, "frame looks uniform — nothing rendered"
print(f"sim-render OK: {len(px)} bytes, {distinct} distinct byte values")
