#!/usr/bin/env python3
"""Uploader: push verified ingests to a cloud remote with rclone, independently
of the ingest daemon. Decoupled on purpose -- a card can be copied, verified,
wiped, and long gone while its local copy is still being uploaded.

It scans dest_base for ingest dirs that are *verified* (have a <ALGO>SUMS
receipt written by the ingest daemon) but *not yet uploaded* (no `.uploaded`
marker), and for each:

    rclone copy  <dir> <remote-base>/<label>-<uuid>/<date>/
    rclone check <dir> <remote-base>/... --one-way    # verify against the
                                                      # remote's own hashes
    rclone sha1sum <remote-base>/... > <dir>/REMOTE_<ALGO>SUMS   # proof
    write <dir>/uploaded.json  (uploaded_at, remote, uploaded_bytes, proof)

The proof is the crux: `rclone check`/`sha1sum` read the hash the backend stores
in object metadata (Google Drive, Backblaze B2, and S3 all serve SHA1/MD5
server-side), so we verify the bytes are really up there **without downloading
them**. The REMOTE_<ALGO>SUMS file is a durable record of what the remote holds;
writing uploaded.json (single writer -- the copier owns metadata.json, we own
this) marks a card safely off site (a local-space reaper, or the display's green
segment, reads it).

Runs once (--once) or loops; drive it from a systemd service/timer. rclone's
remote + credentials come from rclone's own config (`rclone config`); this only
needs the destination base in [remote].
"""
import argparse
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ingest_config import config_paths, human_bytes, load_config, setup_logging
from ingest_copier import (manifest_name, read_metadata, read_uploaded,
                           write_uploaded)

log = logging.getLogger("uploader")



def ready_dirs(base):
    """Yield dest_base/<label-uuid>/<date>/ dirs that are verified (have the
    copier's metadata.json) but not yet uploaded (no uploaded.json)."""
    for card in sorted(_listdir(base)):
        cd = os.path.join(base, card)
        for date in sorted(_listdir(cd)):
            d = os.path.join(cd, date)
            if read_metadata(d) and not read_uploaded(d):
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
    write uploaded.json. Returns True only if the remote provably holds the
    files."""
    rel = os.path.relpath(d, base)
    target = remote_base.rstrip("/") + "/" + rel
    nbytes = read_metadata(d).get("total_bytes", 0)
    log.info("%s: uploading %s -> %s", rel, human_bytes(nbytes), target)
    if _rclone(["copy", d, target]) != 0:
        log.error("%s: rclone copy failed", rel)
        return False
    if _rclone(["check", d, target, "--one-way"]) != 0:
        log.error("%s: rclone check failed -- remote does not match, not marking",
                  rel)
        return False
    proof = os.path.join(d, "REMOTE_" + manifest_name(algo))
    with open(proof + ".tmp", "w") as fo:
        if _rclone([algo + "sum", target], stdout=fo) != 0:
            os.remove(proof + ".tmp")
            log.error("%s: could not read remote hashes for proof", rel)
            return False
    os.replace(proof + ".tmp", proof)         # what the remote actually holds
    write_uploaded(d, {                        # single-writer; presence == done
        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "remote": target,
        "uploaded_bytes": nbytes,
        "proof": "REMOTE_" + manifest_name(algo),
    })
    log.info("%s: uploaded & verified against remote (%s)", rel,
             human_bytes(nbytes))
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", help="one TOML config, replacing the default "
                    "./ingest.toml + ./config.toml layering")
    ap.add_argument("--once", action="store_true", help="one sweep, then exit")
    ap.add_argument("--interval", type=float, default=60,
                    help="seconds between sweeps (loop mode)")
    args = ap.parse_args()
    setup_logging()

    cfg = load_config(*config_paths(args.config))   # ingest.toml + config.toml
    base = cfg["dest"]["base"]
    algo = cfg["hash"]["algo"]
    remote_base = cfg.get("remote", {}).get("base", "")
    if not remote_base:
        log.warning("no [remote] base configured; nothing to do")
        return
    log.info("uploader: %s -> %s (every %gs)", base, remote_base, args.interval)

    while True:
        for d in ready_dirs(base):
            upload_dir(d, base, remote_base, algo)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
