#!/usr/bin/env python3
"""Uploader: push verified ingests to a cloud remote with rclone, independently
of the ingest daemon. Decoupled on purpose -- a card can be copied, verified,
wiped, and long gone while its local copy is still being uploaded.

It scans dest_base for ingest dirs that are *verified* (have a <ALGO>SUMS
receipt written by the ingest daemon) but *not yet uploaded* (no `.uploaded`
marker), and for each:

    rclone copy  <dir> <remote-base>/<uuid>/<date>/
    rclone check <dir> <remote-base>/... --one-way    # verify against the
                                                      # remote's own hashes
    rclone sha1sum <remote-base>/... > <dir>/REMOTE_<ALGO>SUMS   # proof
    touch <dir>/.uploaded

The proof is the crux: `rclone check`/`sha1sum` read the hash the backend stores
in object metadata (Google Drive, Backblaze B2, and S3 all serve SHA1/MD5
server-side), so we verify the bytes are really up there **without downloading
them**. The REMOTE_<ALGO>SUMS file is a durable record of what the remote holds;
`.uploaded` is the marker that says "this card is safely off site" (so a
local-space reaper can later reclaim it).

Runs once (--once) or loops; drive it from a systemd service/timer. rclone's
remote + credentials come from rclone's own config (`rclone config`); this only
needs the destination base in [remote].
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ingest_config import load_config
from ingest_copier import manifest_name

UPLOADED = ".uploaded"


def ready_dirs(base, receipt):
    """Yield dest_base/<uuid>/<date>/ dirs that are verified but not uploaded."""
    for uuid in sorted(_listdir(base)):
        ud = os.path.join(base, uuid)
        for date in sorted(_listdir(ud)):
            d = os.path.join(ud, date)
            if (os.path.isfile(os.path.join(d, receipt))
                    and not os.path.exists(os.path.join(d, UPLOADED))):
                yield d


def _listdir(path):
    try:
        return [n for n in os.listdir(path) if os.path.isdir(os.path.join(path, n))]
    except OSError:
        return []


def _rclone(args, stdout=subprocess.DEVNULL):
    return subprocess.run(["rclone"] + args, stdout=stdout,
                          stderr=subprocess.DEVNULL).returncode


def upload_dir(d, base, remote_base, algo):
    """Copy -> verify against the remote's metadata hashes -> record proof ->
    mark uploaded. Returns True only if the remote provably holds the files."""
    target = remote_base.rstrip("/") + "/" + os.path.relpath(d, base)
    if _rclone(["copy", d, target]) != 0:
        return False
    if _rclone(["check", d, target, "--one-way"]) != 0:
        return False                          # remote doesn't match -> not done
    proof = os.path.join(d, "REMOTE_" + manifest_name(algo))
    with open(proof + ".tmp", "w") as fo:
        if _rclone([algo + "sum", target], stdout=fo) != 0:
            os.remove(proof + ".tmp")
            return False
    os.replace(proof + ".tmp", proof)         # what the remote actually holds
    open(os.path.join(d, UPLOADED), "w").close()
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", help="TOML config (see host/ingest.toml)")
    ap.add_argument("--once", action="store_true", help="one sweep, then exit")
    ap.add_argument("--interval", type=float, default=60,
                    help="seconds between sweeps (loop mode)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    base = cfg["dest"]["base"]
    algo = cfg["hash"]["algo"]
    remote_base = cfg.get("remote", {}).get("base", "")
    if not remote_base:
        print("uploader: no [remote] base configured; nothing to do",
              file=sys.stderr)
        return
    receipt = manifest_name(algo)

    while True:
        for d in ready_dirs(base, receipt):
            print("uploader: uploading %s -> %s" % (d, remote_base),
                  file=sys.stderr)
            ok = upload_dir(d, base, remote_base, algo)
            print("uploader: %s %s" % (d, "uploaded" if ok else "FAILED"),
                  file=sys.stderr)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
