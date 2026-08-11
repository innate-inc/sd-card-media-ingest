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

Beside that card pipeline it also mirrors the plain directories listed in
[backup] paths -- things with no card, no display and no wipe behind them
(an Immich library, a hand-dropped folder). Those get a plain copy + check and
have NOTHING written back into them; see backup_dir() for why.

Runs once (--once) or loops; drive it from a systemd service/timer. rclone's
remote + credentials come from rclone's own config (`rclone config`); this only
needs the destination base in [remote].
"""
import argparse
import logging
import os
import subprocess
import sys
import threading
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


def _rclone_copy(d, target, on_bytes, exclude="*.partial"):
    """rclone copy, streaming --stats so we can report live uploaded bytes.

    exclude defaults to the copier's in-flight *.partial temps, so only whole
    files go up. Backup paths pass exclude=None: we don't own those directories,
    a file legitimately named *.partial would be skipped by the copy and then
    fail the (unfiltered) check on every sweep forever.
    """
    p = subprocess.Popen(
        ["rclone", "copy", d, target]
        + (["--exclude", exclude] if exclude else [])
        + ["--use-json-log", "--stats", "1s", "--stats-log-level", "NOTICE"],
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


def backup_targets(cfg):
    """[backup] paths -> [(src, dst)], dropping malformed entries with a loud
    log.

    Every field is type-checked rather than coerced. This parses operator-edited
    TOML, and an uncaught TypeError here would crash the daemon at startup;
    systemd's Restart=on-failure would then crash-loop it, taking down CARD
    uploading too -- which stalls the display's green segment and therefore the
    wipe interlock. A bad backup line must never cost a card its upload.
    """
    entries = cfg.get("backup", {}).get("paths", []) or []
    if not isinstance(entries, list):
        log.error("[backup] paths must be a list of {src=..., dst=...} tables, "
                  "got %s; ignoring all backup paths", type(entries).__name__)
        return []
    out = []
    for i, ent in enumerate(entries):
        if not isinstance(ent, dict):
            log.error("[backup] paths[%d]: expected a {src=..., dst=...} table, "
                      "got %s; skipping", i, type(ent).__name__)
            continue
        src, dst = ent.get("src"), ent.get("dst")
        if not isinstance(src, str) or not isinstance(dst, str):
            log.error("[backup] paths[%d]: src and dst must both be strings; "
                      "skipping", i)
            continue
        src, dst = src.rstrip("/"), dst.strip("/")
        if not src or not dst:
            log.error("[backup] paths[%d]: needs both src and dst; skipping", i)
            continue
        if ".." in dst.split("/"):
            log.error("[backup] paths[%d]: dst must stay under the remote base "
                      "(no '..'); skipping", i)
            continue
        out.append((src, dst))
    return out


def _backup_seconds(cfg, key):
    """A [backup] duration in seconds. 0, negative or unparseable -> 0.0, always
    said out loud -- same reasoning as backup_targets: never raise."""
    raw = cfg.get("backup", {}).get(key, 0)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        log.error("[backup] %s %r is not a number; treating as 0", key, raw)
        return 0.0
    return v if v > 0 else 0.0


def backup_every_seconds(cfg):
    """Seconds between copy passes. 0 = no backup sweeps at all."""
    return _backup_seconds(cfg, "interval")


def backup_verify_seconds(cfg):
    """Seconds between full hash scrubs. 0 = re-hash on every pass, which is
    the conservative reading of an unset or malformed value."""
    return _backup_seconds(cfg, "verify_every")


def backup_dir(src, dst, remote_base, scrub=True):
    """Mirror one plain directory to the remote and verify it against the
    remote's own hashes.

    Unlike a card ingest there is no copier metadata, no display segment and no
    wipe interlock, so this writes NOTHING into src -- no uploaded.json, no
    REMOTE_SHA1SUMS. These directories are owned by another application (Immich
    indexes its library; the container writes its own DB dumps), and rclone copy
    is idempotent, so the remote is the state.

    The copy pass is a listing diff and costs ~nothing; the check re-reads every
    byte of src, so it is the half worth throttling. scrub=False skips it when
    the copy moved nothing -- the remote cannot have drifted from a side we did
    not write and nothing else writes there. A copy that DID move bytes is
    always checked, whatever scrub says, so new data is never left unverified.

    Returns "verified" (hash-checked against the remote), "unchanged" (nothing
    moved, not re-hashed this pass), or "" if the path is not safely backed up.
    """
    target = remote_base.rstrip("/") + "/" + dst
    try:
        if not os.path.isdir(src):
            # Fail closed: an unmounted volume must never read as "empty". copy
            # (never sync) means the remote keeps whatever it already holds.
            log.error("backup %s: source missing -- skipping, %s left untouched",
                      src, target)
            return False
        if not os.listdir(src):
            # An empty dir is indistinguishable from a mountpoint whose volume
            # is gone, so it is never reported verified. Nothing is deleted
            # either way (copy, never sync); this only stops a lying log.
            log.warning("backup %s: source is EMPTY -- skipping rather than "
                        "reporting it verified (volume not mounted?)", src)
            return False
    except OSError as e:
        log.error("backup %s: unreadable (%s) -- skipping", src, e)
        return False
    # exclude=None: we do not own this directory, so nothing may be filtered out
    # of the copy that the check would then demand be present.
    moved = [0]
    if _rclone_copy(src, target, lambda b: moved.__setitem__(0, b),
                    exclude=None) != 0:
        log.error("backup %s: rclone copy failed", src)
        return ""
    if not (scrub or moved[0]):
        log.info("backup %s -> %s: up to date (nothing new; not re-hashed)",
                 src, target)
        return "unchanged"
    if _rclone(["check", src, target, "--one-way"]) != 0:
        log.error("backup %s: rclone check failed -- remote does not match", src)
        return ""
    log.info("backup %s -> %s: up to date and verified%s", src, target,
             (" (%s new)" % human_bytes(moved[0])) if moved[0] else "")
    return "verified"


def backup_sweep(cfg, remote_base, verify_every=0, last=None):
    """One pass over [backup] paths. Returns (ok, total).

    A path is hash-checked when its copy moved bytes, or when its last check is
    older than verify_every. last maps dst -> time.monotonic() of the last
    successful check and is updated in place; pass None (the default) to check
    every path on every pass. It is deliberately in-memory only: losing it to a
    restart costs one extra scrub, which is the harmless direction.
    """
    targets = backup_targets(cfg)
    now = time.monotonic()
    ok = 0
    for src, dst in targets:
        due = last is None or not verify_every or \
            now - last.get(dst, float("-inf")) >= verify_every
        res = backup_dir(src, dst, remote_base, scrub=due)
        if res:
            ok += 1
        if res == "verified" and last is not None:
            last[dst] = now
    return ok, len(targets)


def _log_sweep(ok, total):
    if total:
        log.info("backup sweep: %d/%d path(s) verified", ok, total)


def _backup_loop(cfg, remote_base, every, verify_every):
    """Sweep on our own clock, off the card loop. Nothing raised in here may
    reach the card path, so the whole body is guarded."""
    last = {}
    while True:
        try:
            _log_sweep(*backup_sweep(cfg, remote_base, verify_every, last))
        except Exception:
            log.exception("backup sweep failed; retrying in %gs", every)
        time.sleep(every)


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
    backup_every = backup_every_seconds(cfg)
    backup_verify = backup_verify_seconds(cfg)
    n_backup = len(backup_targets(cfg))
    if n_backup and not backup_every:
        log.warning("backup: %d path(s) configured but interval is 0 -- "
                    "no backups will run", n_backup)
    elif n_backup and not args.once:
        log.info("backup: %d plain path(s) every %gs, full verify every %gs",
                 n_backup, backup_every, backup_verify)
        # Its own thread: a first full mirror can run for hours, and the card
        # sweep is latency-critical (the green segment gates a destructive
        # wipe), so the two must never share a thread. daemon=True so this
        # never holds up process exit.
        threading.Thread(target=_backup_loop,
                         args=(cfg, remote_base, backup_every, backup_verify),
                         daemon=True).start()

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
            if backup_every:            # --once means "do everything once"
                _log_sweep(*backup_sweep(cfg, remote_base))
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
