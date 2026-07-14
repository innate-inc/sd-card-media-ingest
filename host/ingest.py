#!/usr/bin/env python3
"""Ingest daemon: discover cards behind the reader hub, copy + verify their
files, drive the device display, and wipe on confirm (see ARCHITECTURE.md).

    nix run .#ingest -- --dry-run | nix run .#sim      # no hardware needed
    nix run .#ingest -- --config host/ingest.toml      # the real thing

The pieces live in sibling modules so the sensitive part is small to review:
  ingest_config    defaults + TOML
  ingest_discovery which slots hold which card
  ingest_copier    copy -> verify -> manifest -> wipe   (the deletion path)
  ingest_emit      model -> device line protocol
  ingest_link      serial (by USB VID/PID) + confirm channel

Files land in  dest_base/<uuid>/<ingest_date>/<relpath>  -- a fresh directory
per ingest, so nothing is overwritten and there is no resume/dedup to reason
about. Wiping needs a device `confirm <i>` AND is a logged dry run unless armed
by both `[wipe] enabled = true` and `--enable-wipe`.
"""
import argparse
import logging
import os
import queue
import sys
import tempfile
import threading
import time

from ingest_config import as_bool, human_bytes, load_config, setup_logging
from ingest_copier import (CardJob, COPYING, IDLE, PENDING, read_uploaded,
                           VERIFYING, WIPING)
from ingest_discovery import HubDiscovery, MockDiscovery, UNKNOWN
from ingest_emit import Emitter
from ingest_link import SerialLink, confirm_reader, find_port

log = logging.getLogger("ingest")

DEFAULT_CONFIG = "ingest.toml"   # in the working dir (project dir), not /etc


def open_display(cfg, args):
    """(rx, tx) for the confirm channel + display. A serial device found by
    VID/PID owns both directions; otherwise stdin/stdout (pipe mode)."""
    if not args.dry_run:
        vid = args.vid if args.vid is not None else cfg["serial"].get("vid", "")
        pid = args.pid if args.pid is not None else cfg["serial"].get("pid", "")
        if vid or pid:
            port = find_port(vid, pid)
            if port:
                log.info("device on %s (%s:%s)", port, vid, pid)
                link = SerialLink(port)
                return link, link
            log.warning("no serial device %s:%s; using stdout/stdin", vid, pid)
    return sys.stdin, sys.stdout


def _auto_confirm(jobs, pending_since, after_s):
    """[--dry-run demo only] confirm a slot S seconds after it goes pending."""
    now = time.monotonic()
    for i, job in jobs.items():
        if job.state == PENDING:
            if now - pending_since.setdefault(i, now) >= after_s:
                job.request_wipe()
        else:
            pending_since.pop(i, None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", help="TOML config (default: ./ingest.toml)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fake cards in a scratch dir; no hardware, no serial")
    ap.add_argument("--vid", help="USB vendor id of the device (overrides config)")
    ap.add_argument("--pid", help="USB product id of the device (overrides config)")
    ap.add_argument("--dest", help="override [dest] base")
    ap.add_argument("--hub-prefix", help="override [hub] path_prefix")
    ap.add_argument("--interval-ms", type=int, help="override [poll] interval_ms")
    ap.add_argument("--ticks", type=int, default=0,
                    help="exit after N ticks (0 = run forever); for tests")
    ap.add_argument("--auto-confirm", type=float, default=0, metavar="S",
                    help="[dry-run only] auto-confirm a pending slot after S s")
    args = ap.parse_args()
    setup_logging()

    config = args.config or (DEFAULT_CONFIG if os.path.exists(DEFAULT_CONFIG)
                             else None)
    cfg = load_config(config)
    log.info("config: %s", config or "(built-in defaults)")
    if args.interval_ms is not None:           # honour an explicit 0, too
        cfg["poll"]["interval_ms"] = args.interval_ms

    # Real deletion needs [wipe] enabled = true in the config (and never in a
    # dry run). Otherwise a confirmed wipe only logs what it would delete.
    wipe_armed = as_bool(cfg["wipe"].get("enabled", False)) and not args.dry_run
    if wipe_armed:
        log.warning("wipe ARMED: confirmed cards will be PERMANENTLY erased")

    if args.dry_run:
        root = tempfile.mkdtemp(prefix="ingest-dry-")
        disco = MockDiscovery(root)
        cfg["dest"]["base"] = args.dest or os.path.join(root, "dest")
        log.info("dry run: fake cards + dest under %s", root)
    else:
        hub = dict(cfg["hub"])
        if args.hub_prefix:                    # explicit prefix overrides vid/pid
            hub["path_prefix"], hub["vid"] = args.hub_prefix, ""
        disco = HubDiscovery(hub)
        cfg["dest"]["base"] = args.dest or cfg["dest"]["base"]
    log.info("dest base: %s ; %d reader slot(s)",
             cfg["dest"]["base"], len(disco.slots()))

    rx, tx = open_display(cfg, args)
    emitter = Emitter(tx, cfg["segments"])
    confirms = queue.Queue()
    threading.Thread(target=confirm_reader, args=(rx, confirms),
                     daemon=True).start()

    emitter.preamble()
    jobs = {}                                  # slot index -> CardJob
    interval = cfg["poll"]["interval_ms"] / 1000.0
    pending_since = {}                         # slot -> t (for --auto-confirm)
    tick = 0

    while True:
        slots = disco.slots()

        # Reconcile discovery with running jobs.
        for i, card in enumerate(slots):
            if card is UNKNOWN:
                continue                       # transient probe error; leave as-is
            job = jobs.get(i)
            if job and (card is None or card.ident != job.card.ident):
                log.info("slot %d: %s removed", i, job.card.label)
                job.abort = True               # stop its worker
                if job.state in (IDLE, COPYING, VERIFYING, WIPING):
                    job.fail("REMOVED")
                del jobs[i]
                job = None
            if card is not None and job is None:
                if card.mountpoint is None:
                    log.warning("slot %d: %s present but not mounted; skipping",
                                i, card.label)
                    continue                   # present but unreadable; skip
                log.info("slot %d: %s inserted (%s, uuid %s)", i, card.label,
                         human_bytes(card.capacity_bytes), card.uuid)
                job = CardJob(card, cfg, wipe_armed=wipe_armed,
                              throttle_bps=1_500_000 if args.dry_run else 0)
                jobs[i] = job
                job.start()

        # Wipe confirmations -- the only path to deletion.
        try:
            while True:
                i = confirms.get_nowait()
                if i in jobs:
                    log.info("slot %d: confirm received -> wipe", i)
                    jobs[i].request_wipe()
                else:
                    log.warning("confirm %d ignored (no card)", i)
        except queue.Empty:
            pass
        if args.dry_run and args.auto_confirm:
            _auto_confirm(jobs, pending_since, args.auto_confirm)

        # Reflect upload progress (the separate uploader writes uploaded.json).
        for job in jobs.values():
            if job.state == PENDING:
                job.uploaded_bytes = read_uploaded(job.dest).get("uploaded_bytes", 0)

        # One display frame. Absent slots keep their column (slot count is fixed).
        emitter.tick([jobs.get(i) for i in range(len(slots))])

        tick += 1
        if args.ticks and tick >= args.ticks:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
