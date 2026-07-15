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

Files land in  dest_base/<label>-<uuid>/<ingest_date>/<relpath>  -- a fresh directory
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

from ingest_config import (as_bool, config_paths, human_bytes, load_config,
                           setup_logging)
from ingest_copier import (CardJob, COPYING, IDLE, PENDING, upload_progress,
                           VERIFYING, WIPING)
from ingest_discovery import HubDiscovery, MockDiscovery, UNKNOWN
from ingest_emit import Emitter
from ingest_link import confirm_reader, ReconnectingSerial

log = logging.getLogger("ingest")


def open_display(cfg, args):
    """Return (read_confirms, out): read_confirms(queue) is the confirm-reader
    thread body; out is the emitter's line sink. A serial display (found by
    VID/PID) transparently reconnects across unplugs; otherwise stdin/stdout
    (pipe / dry-run mode)."""
    if not args.dry_run:
        vid = args.vid if args.vid is not None else cfg["serial"].get("vid", "")
        pid = args.pid if args.pid is not None else cfg["serial"].get("pid", "")
        if vid or pid:
            link = ReconnectingSerial(vid, pid)
            return link.read_confirms, link
    return (lambda q: confirm_reader(sys.stdin, q)), sys.stdout


def _free_col(used):
    """Lowest display-column index not currently taken (fills gaps left by
    removed cards, so the list stays compact)."""
    c = 0
    while c in used:
        c += 1
    return c


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
    ap.add_argument("--config", help="one TOML config, replacing the default "
                    "./ingest.toml + ./config.toml layering")
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

    paths = config_paths(args.config)     # base ingest.toml + local config.toml
    cfg = load_config(*paths)
    loaded = [p for p in paths if os.path.exists(p)]
    log.info("config: %s", " + ".join(loaded) or "(built-in defaults)")
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
        disco = HubDiscovery(hub, mount=True)  # headless: mount cards ourselves
        cfg["dest"]["base"] = args.dest or cfg["dest"]["base"]
    log.info("dest base: %s ; %d reader slot(s)",
             cfg["dest"]["base"], len(disco.slots()))

    read_confirms, tx = open_display(cfg, args)
    emitter = Emitter(tx, cfg["segments"])
    confirms = queue.Queue()
    threading.Thread(target=read_confirms, args=(confirms,), daemon=True).start()

    emitter.preamble()
    # A reconnecting serial display comes up blank; re-send the preamble (bg,
    # legend, numbers) whenever it (re)connects. Pipe mode has no such method.
    display_reconnected = getattr(tx, "take_reconnected", None)
    # The display is a list of the cards plugged in, in insertion order: each
    # gets the lowest free display column when it appears and keeps it (so its
    # confirm number never shifts under it) until removal, which frees it again.
    jobs = {}                                  # physical slot index -> CardJob
    used_cols = set()                          # display columns currently taken
    unmounted_warned = set()                   # slots warned about (not mounted)
    released = set()                           # idents unmounted after their wipe
    interval = cfg["poll"]["interval_ms"] / 1000.0
    pending_since = {}                         # slot -> t (for --auto-confirm)
    tick = 0

    while True:
        if display_reconnected and display_reconnected():
            emitter.preamble()

        slots = disco.slots()

        # Reconcile discovery with running jobs.
        for i, card in enumerate(slots):
            if card is UNKNOWN:
                continue                       # transient probe error; leave as-is
            job = jobs.get(i)
            if job and (card is None or card.ident != job.card.ident):
                log.info("slot %d: %s removed", job.col, job.card.label)
                job.abort = True               # stop its worker
                if job.state in (IDLE, COPYING, VERIFYING, WIPING):
                    job.fail("REMOVED")
                used_cols.discard(job.col)
                released.discard(job.card.ident)
                del jobs[i]
                job = None
            if card is None:
                unmounted_warned.discard(i)    # slot empty again; re-arm the warning
            if card is not None and job is None:
                if card.mountpoint is None:
                    if i not in unmounted_warned:   # once, not every tick
                        unmounted_warned.add(i)
                        log.warning("%s present but could not be mounted; "
                                    "skipping", card.label)
                    continue                   # present but unreadable; skip
                unmounted_warned.discard(i)
                col = _free_col(used_cols)
                used_cols.add(col)
                log.info("slot %d: %s inserted (%s, uuid %s)", col, card.label,
                         human_bytes(card.capacity_bytes), card.uuid)
                job = CardJob(card, cfg, wipe_armed=wipe_armed,
                              throttle_bps=1_500_000 if args.dry_run else 0)
                job.col = col
                jobs[i] = job
                job.start()

        by_col = {job.col: job for job in jobs.values()}

        # Wipe confirmations -- the only path to deletion. The confirm index is
        # the display column the operator selected, so it targets what they saw.
        try:
            while True:
                c = confirms.get_nowait()
                if c in by_col:
                    log.info("slot %d: confirm received -> wipe", c)
                    by_col[c].request_wipe()
                else:
                    log.warning("confirm %d ignored (no card)", c)
        except queue.Empty:
            pass
        if args.dry_run and args.auto_confirm:
            _auto_confirm(jobs, pending_since, args.auto_confirm)

        # Reflect upload progress (the uploader streams it live -- even mid-copy,
        # so the green segment grows while the card is still being copied).
        for job in jobs.values():
            if job.state in (COPYING, VERIFYING, PENDING):
                job.uploaded_bytes = upload_progress(job.dest)

        # A really-wiped card is done: unmount it (once) so it's flushed and
        # safe to pull. (A dry-run confirm deleted nothing, so leave it mounted.)
        for job in jobs.values():
            if (job.wiped and job.wipe_armed
                    and job.card.ident not in released):
                released.add(job.card.ident)
                disco.release(job.card.ident)
                log.info("slot %d: %s wiped -- unmounted, safe to remove",
                         job.col, job.card.label)

        # One display frame: the present cards in column order.
        ncols = max(by_col) + 1 if by_col else 0
        emitter.tick([by_col.get(c) for c in range(ncols)])

        tick += 1
        if args.ticks and tick >= args.ticks:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
