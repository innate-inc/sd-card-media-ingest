#!/usr/bin/env python3
"""List the drives plugged into the reader hub right now (hub port, device,
and any card in it), in physical-port order, then exit. READ-ONLY: never copies
or wipes -- a diagnostic to check the [hub] match and see what's detected while
you plug cards in. (The live display numbers cards by insertion order, not by
these ports.)

    nix run .#slots           # uses ./ingest.toml (the [hub] vid:pid)
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ingest_config import config_paths, human_bytes, load_config
from ingest_discovery import HubDiscovery, UNKNOWN


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", help="one TOML config, replacing the default "
                    "./ingest.toml + ./config.toml layering")
    args = ap.parse_args()
    cfg = load_config(*config_paths(args.config))   # ingest.toml + config.toml

    disco = HubDiscovery(cfg["hub"])
    probed = disco.slots()
    print("%-4s %-8s %-5s %-10s %s" % ("#", "port", "dev", "size", "card"))
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
