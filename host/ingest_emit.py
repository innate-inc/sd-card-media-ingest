"""Emitter: jobs -> device line protocol, once per tick (see ARCHITECTURE.md).

Segments are permille of each card's OWN capacity (relative scale): uploaded
(copied + verified) / copied-not-yet-verified / uncopied stack from the bottom;
the leftover is free space in the `bg` colour.
"""
import os
import sys
import time

from ingest_config import as_bool, color
from ingest_copier import (COPYING, EMPTY, ERROR, IDLE, PENDING, VERIFYING,
                           WIPING)


class Emitter:
    def __init__(self, out, seg_cfg):
        self.out = out
        self.up = color(seg_cfg["uploaded"])
        self.cop = color(seg_cfg["copied"])
        self.unc = color(seg_cfg["uncopied"])
        self.bg = color(seg_cfg["empty"])
        self.numbers = 1 if as_bool(seg_cfg.get("numbers", True)) else 0
        self._paths = {}                       # last `path` sent per slot
        self._last_warn = 0.0

    def emit(self, line):
        try:
            self.out.write(line + "\n")
        except BrokenPipeError:
            self._display_gone()
        except OSError as e:
            self._link_hiccup(e)

    def flush(self):
        try:
            self.out.flush()
        except BrokenPipeError:
            self._display_gone()
        except OSError as e:
            self._link_hiccup(e)

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
            print("ingest: display write error (skipping frame): %s" % e,
                  file=sys.stderr)

    def preamble(self):
        self.emit("clear")
        self.emit("bg %06x" % self.bg)
        self.emit("numbers %d" % self.numbers)
        self.emit("legend clear")
        self.emit("legend %06x uploaded" % self.up)
        self.emit("legend %06x copied" % self.cop)
        self.emit("legend %06x uncopied" % self.unc)
        self.emit("legend %06x free space" % self.bg)

    def tick(self, jobs):
        """jobs: list of CardJob-or-None in fixed physical slot order."""
        for i, job in enumerate(jobs):
            # an absent reader and a wiped slot are the same blank row
            if job is None or job.state == EMPTY:
                self.emit("slot %d -1 -1 idle 0 0 0 0 0 0 0 0 empty" % i)
                continue
            self._path(i, job)
            self.emit(self._slot_line(i, job))
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
        up = pm(job.verified_bytes)
        cop = pm(job.copied_bytes - job.verified_bytes)
        unc = pm(job.total_bytes - job.copied_bytes)
        size_mb = cap // 1_000_000
        status, label = {
            IDLE:      ("idle", job.card.label),
            COPYING:   ("active", job.card.label),
            VERIFYING: ("active", job.card.label),
            PENDING:   ("pending", job.card.label),
            WIPING:    ("active", "WIPING"),
            ERROR:     ("error", job.error or "ERROR"),
        }.get(job.state, ("idle", job.card.label))
        # eta is -1 (unknown): the device just shows the name/size label.
        return ("slot %d %d -1 %s %d %06x %d %06x %d %06x 0 0 %s"
                % (i, size_mb, status, up, self.up, cop, self.cop,
                   unc, self.unc, label[:23]))
