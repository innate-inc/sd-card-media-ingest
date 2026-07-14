#!/usr/bin/env python3
"""Print the reader-slot mapping (slot -> hub port -> device -> what's in it)
and exit. READ-ONLY: never copies or wipes -- run it to see how the physical
ports map to slot numbers while you plug cards/drives in.

    nix run .#slots           # uses ./ingest.toml (the [hub] vid:pid)
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ingest_config import human_bytes, load_config
from ingest_discovery import HubDiscovery, UNKNOWN

DEFAULT_CONFIG = "ingest.toml"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", help="TOML config (default: ./ingest.toml)")
    args = ap.parse_args()
    config = args.config or (DEFAULT_CONFIG if os.path.exists(DEFAULT_CONFIG)
                             else None)
    cfg = load_config(config)

    disco = HubDiscovery(cfg["hub"])
    probed = disco.slots()
    print("%-4s %-8s %-5s %-10s %s" % ("slot", "port", "dev", "size", "card"))
    print("-" * 48)
    for i, (ident, card) in enumerate(zip(disco.slot_ids, probed), start=1):
        m = re.search(r"-usb-\d+:([\d.]+):", ident)
        port = m.group(1) if m else "?"
        dev = os.path.basename(
            os.path.realpath(os.path.join(HubDiscovery.BY_PATH, ident)))
        if card is None:
            size, what = "-", "(empty)"
        elif card is UNKNOWN:
            size, what = "-", "(unreadable)"
        else:
            size, what = human_bytes(card.capacity_bytes), card.label
        print("%-4d %-8s %-5s %-10s %s" % (i, port, dev, size, what))
    if not disco.slot_ids:
        print("(no drives found behind the configured hub -- check [hub] in "
              "%s, or `lsusb`)" % (config or "the built-in defaults"))


if __name__ == "__main__":
    main()
