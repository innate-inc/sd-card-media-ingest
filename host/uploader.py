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
from ingest_copier import (_stats_bytes, clear_uploading, is_copying,
                           manifest_name, read_metadata, read_uploaded,
                           upload_progress, write_uploaded, write_uploading)

log = logging.getLogger("uploader")



def ready_dirs(base):
    """Yield dest_base/<label-uuid>/<date>/ dirs to push: verified (metadata.json)
    OR still being written by the copier (a "<dir>.copying" marker), as long as
    they aren't already finished (no uploaded.json)."""
    for card in sorted(_listdir(base)):
        cd = os.path.join(base, card)
        for date in sorted(_listdir(cd)):
            d = os.path.join(cd, date)
            if read_uploaded(d):
                continue
            if read_metadata(d) or is_copying(d):
                yield d


def _listdir(path):
    try:
        return [n for n in os.listdir(path) if os.path.isdir(os.path.join(path, n))]
    except OSError:
        return []


def _rclone(args, stdout=subprocess.DEVNULL):
    return subprocess.run(["rclone"] + args, stdout=stdout,
                          stderr=subprocess.DEVNULL).returncode


def _rclone_copy(d, target, on_bytes):
    """rclone copy, streaming --stats so we can report live uploaded bytes.
    Excludes the copier's in-flight *.partial temps -- only whole files go up."""
    p = subprocess.Popen(
        ["rclone", "copy", d, target, "--exclude", "*.partial",
         "--use-json-log", "--stats", "1s", "--stats-log-level", "NOTICE"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for line in p.stderr:
        b = _stats_bytes(line)
        if b is not None:
            on_bytes(b)
    return p.wait()


def upload_dir(d, base, remote_base, algo):
    """Push the fully-copied files to the remote (streaming, so the display's
    "uploaded" segment fills live). While the copier is still writing the dir
    this just mirrors what's complete; once it's verified (metadata.json), it
    also checks against the remote's hashes, records proof, and writes
    uploaded.json. Returns True only when that finalisation succeeds."""
    rel = os.path.relpath(d, base)
    target = remote_base.rstrip("/") + "/" + rel
    meta = read_metadata(d)                    # present => copy+verify finished
    # rclone --stats reports bytes for THIS pass only (already-uploaded files are
    # skipped -> 0), so add them to the running total already up. Monotonic, and
    # survives an uploader restart (the .uploading file is on the backup disk).
    base = upload_progress(d)
    if _rclone_copy(d, target, lambda b: write_uploading(d, base + b)) != 0:
        clear_uploading(d)
        log.error("%s: rclone copy failed", rel)
        return False
    if not meta:
        return False                           # still copying; pushed what's ready
    nbytes = meta.get("total_bytes", 0)
    if _rclone(["check", d, target, "--one-way"]) != 0:
        clear_uploading(d)
        log.error("%s: rclone check failed -- remote does not match, not marking",
                  rel)
        return False
    proof = os.path.join(d, "REMOTE_" + manifest_name(algo))
    with open(proof + ".tmp", "w") as fo:
        if _rclone([algo + "sum", target], stdout=fo) != 0:
            os.remove(proof + ".tmp")
            clear_uploading(d)
            log.error("%s: could not read remote hashes for proof", rel)
            return False
    os.replace(proof + ".tmp", proof)         # what the remote actually holds
    write_uploaded(d, {                        # single-writer; presence == done
        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "remote": target,
        "uploaded_bytes": nbytes,
        "proof": "REMOTE_" + manifest_name(algo),
    })
    clear_uploading(d)                         # done marker lands; drop live file
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

    heartbeat_every = max(1, round(600 / max(args.interval, 1)))    # ~10 min
    tick = 0
    while True:
        ready = list(ready_dirs(base))
        done = 0
        for d in ready:
            if upload_dir(d, base, remote_base, algo):   # logs per dir on finalise
                done += 1
        if tick % heartbeat_every == 0:          # periodic liveness, reflecting state
            n = len(ready) - done                # dirs pushed but not yet verified/final
            log.info("watching %s -- %s", base,
                     ("%d dir(s) in flight" % n) if n else "nothing to upload")
        tick += 1
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
