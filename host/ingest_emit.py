"""Emitter: jobs -> device line protocol, once per tick (see ARCHITECTURE.md).

Segments are permille of each card's OWN capacity (relative scale): uploaded
(copied + verified) / copied-not-yet-verified / uncopied stack from the bottom;
the leftover is free space in the `bg` colour.
"""
import logging
import os
import time

from ingest_config import as_bool, color

log = logging.getLogger("ingest")
from ingest_copier import (COPYING, EMPTY, ERROR, IDLE, PENDING, VERIFYING,
                           WIPING)

# job state -> protocol status word (built once). EMPTY is handled in tick().
_STATUS = {IDLE: "idle", COPYING: "active", VERIFYING: "active",
           PENDING: "pending", WIPING: "active", ERROR: "error"}


class Emitter:
    def __init__(self, out, seg_cfg):
        self.out = out
        self.uncopied = color(seg_cfg["uncopied"])
        self.copied = color(seg_cfg["copied"])
        self.verified = color(seg_cfg["verified"])
        self.uploaded = color(seg_cfg["uploaded"])
        self.bg = color(seg_cfg["empty"])
        self.numbers = 1 if as_bool(seg_cfg.get("numbers", True)) else 0
        self._paths = {}                       # last `path` sent per column
        self._drawn = set()                    # columns with a bar drawn last tick
        self._last_warn = 0.0

    def emit(self, line):
        self._safe(lambda: self.out.write(line + "\n"))

    def flush(self):
        self._safe(self.out.flush)

    def _safe(self, fn):
        try:
            fn()
        except BrokenPipeError:
            self._display_gone()          # pipe reader gone -> exit quietly
        except OSError as e:
            self._link_hiccup(e)          # serial glitch -> warn + skip frame

    @staticmethod
    def _display_gone():
        """The pipe reader (sim / stdout) went away; exit quietly. os._exit
        skips interpreter shutdown, which would re-flush dead stdout."""
        os._exit(0)

    def _link_hiccup(self, e):
        """A non-fatal write error (a serial glitch): warn, rate-limited, and
        skip this frame rather than crash mid-ingest."""
        now = time.monotonic()
        if now - self._last_warn > 5:
            self._last_warn = now
            log.warning("display write error (skipping frame): %s", e)

    def preamble(self):
        self.emit("clear")
        self.emit("bg %06x" % self.bg)
        self.emit("numbers %d" % self.numbers)
        self.emit("legend clear")
        self.emit("legend %06x uploaded" % self.uploaded)
        self.emit("legend %06x verified" % self.verified)
        self.emit("legend %06x copied" % self.copied)
        self.emit("legend %06x uncopied" % self.uncopied)
        self.emit("legend %06x free space" % self.bg)

    def tick(self, jobs):
        """jobs: a list of CardJob-or-None indexed by display column (the cards
        currently plugged in, in the order they were inserted). A column that
        held a bar last tick but is gone now is blanked once so the device
        clears it -- even if the list has since shrunk past it."""
        drawn = set()
        n = len(jobs)
        hi = max([n] + [c + 1 for c in self._drawn])   # also clear freed columns
        for c in range(hi):
            job = jobs[c] if c < n else None
            if job is None or job.state == EMPTY:
                if c < n or c in self._drawn:      # in-range gap, or a freed column
                    self.emit("slot %d -1 -1 idle 0 0 0 0 0 0 0 0 empty" % c)
                continue
            self._path(c, job)
            self.emit(self._slot_line(c, job))
            drawn.add(c)
        self._drawn = drawn
        self.emit("hb")
        self.flush()

    def _path(self, i, job):
        detail = job.dest[:47]                 # MAX_DETAIL is 48 incl. NUL
        if self._paths.get(i) != detail:
            self._paths[i] = detail
            self.emit("path %d %s" % (i, detail))

    def _slot_line(self, i, job):
        cap = max(job.card.capacity_bytes, 1)
        pm = lambda b: max(0, min(1000, round(b * 1000 / cap)))
        # four stacked stages, most-done at the bottom (p0):
        uploaded = pm(job.uploaded_bytes)
        verified = pm(job.verified_bytes - job.uploaded_bytes)
        copied = pm(job.copied_bytes - job.verified_bytes)
        uncopied = pm(job.total_bytes - job.copied_bytes)
        size_mb = cap // 1_000_000
        status = _STATUS.get(job.state, "idle")
        if job.state == WIPING:
            label = "WIPING"
        elif job.state == ERROR:
            label = job.error or "ERROR"
        else:
            label = job.card.label
        # eta is -1 (unknown): the device just shows the name/size label.
        return ("slot %d %d -1 %s %d %06x %d %06x %d %06x %d %06x %s"
                % (i, size_mb, status, uploaded, self.uploaded,
                   verified, self.verified, copied, self.copied,
                   uncopied, self.uncopied, label[:23]))
