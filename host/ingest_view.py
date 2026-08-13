"""View model: jobs -> the numbers the web page renders.

This was the device line-protocol emitter. The maths is unchanged -- only the
output target moved, from a serial protocol the firmware parsed to a dict the
HTML renderer walks.

Segments are permille of each card's OWN capacity (relative scale): uploaded
(copied + verified) / copied-not-yet-verified / uncopied stack from the bottom;
the leftover is free space in the `bg` colour.
"""
import logging
import time

from ingest_config import as_bool, color
from ingest_copier import (COPYING, EMPTY, ERROR, IDLE, PENDING, VERIFYING,
                           WIPING)

log = logging.getLogger("ingest")

# job state -> status word (built once). EMPTY is handled in cards().
_STATUS = {IDLE: "idle", COPYING: "active", VERIFYING: "active",
           PENDING: "pending", WIPING: "active", ERROR: "error",
           EMPTY: "empty"}


def css(v):
    """'#RRGGBB' / 'RRGGBB' / int -> '#rrggbb', for a CSS colour literal."""
    return "#%06x" % color(v)


class View:
    """Turns CardJobs into render-ready dicts. One instance per daemon, because
    it carries the copy-rate EMA between ticks."""

    def __init__(self, seg_cfg):
        self.uncopied = css(seg_cfg["uncopied"])
        self.copied = css(seg_cfg["copied"])
        self.verified = css(seg_cfg["verified"])
        self.uploaded = css(seg_cfg["uploaded"])
        self.bg = css(seg_cfg["empty"])
        self.numbers = as_bool(seg_cfg.get("numbers", True))
        self._rate = {}                        # job.dest -> (t, copied_bytes, ema)

    def legend(self):
        """(colour, label) pairs, most-done first -- the same order the bars
        stack, so the legend reads bottom-up like the bar does."""
        return [(self.uploaded, "uploaded"), (self.verified, "verified"),
                (self.copied, "copied"), (self.uncopied, "uncopied"),
                (self.bg, "free space")]

    def cards(self, jobs):
        """jobs: a list of CardJob-or-None indexed by display column (the cards
        currently plugged in, in insertion order). Returns one dict per occupied
        column; empty columns are simply absent."""
        out = []
        for c, job in enumerate(jobs):
            if job is None:
                continue                       # nothing in that reader at all
            out.append(self._card(c, job))
        return out

    def _card(self, i, job):
        cap = max(job.card.capacity_bytes, 1)
        pm = lambda b: max(0, min(1000, round(b * 1000 / cap)))
        # Four stacked stages, most-done first. Round *cumulative* boundaries
        # (each >= the last) and take differences, so the segments always sum to
        # exactly pm(total_bytes). Rounding each stage's delta independently
        # instead makes the sum -- and thus the "free" leftover -- jitter by a
        # permille every tick as the copied/uncopied split moves.
        b_up = pm(job.uploaded_bytes)
        b_ver = max(b_up, pm(job.verified_bytes))
        b_cop = max(b_ver, pm(job.copied_bytes))
        b_tot = max(b_cop, pm(job.total_bytes))
        segments = [
            ("uploaded", b_up, self.uploaded),
            ("verified", b_ver - b_up, self.verified),
            ("copied", b_cop - b_ver, self.copied),
            ("uncopied", b_tot - b_cop, self.uncopied),
        ]
        eta_s, kbps = self._eta_kbps(job)
        state = job.state
        if state == WIPING:
            label = "WIPING"
        elif state == ERROR:
            label = job.error or "ERROR"
        else:
            label = job.card.label
        return {
            "col": i,
            # A card that mounted but holds no files. Rendered as a present-but
            # -empty slot rather than omitted, so "card in reader with nothing
            # on it" cannot be mistaken for "no card in reader".
            "empty": state == EMPTY,
            "uuid": job.card.uuid or "",
            "label": label,
            "status": _STATUS.get(state, "idle"),
            # Only a PENDING card may be confirmed -- the same interlock the
            # device button had. The renderer shows a confirm form iff this.
            "pending": state == PENDING,
            "size_mb": cap // 1_000_000,
            "eta_s": eta_s,
            "kbps": kbps,
            "segments": [s for s in segments if s[1] > 0],
            "free": 1000 - b_tot,
            "path": job.dest,
            "wipe_armed": bool(getattr(job, "wipe_armed", False)),
        }

    def _eta_kbps(self, job):
        """Smoothed copy rate -> (eta_s, kbps); (-1, -1) when not measurable.
        Sampled at tick cadence from copied_bytes with an EMA so the numbers
        don't jump around."""
        if job.state != COPYING:
            self._rate.pop(job.dest, None)
            return -1, -1
        now, done = time.monotonic(), job.copied_bytes
        prev = self._rate.get(job.dest)
        ema = 0.0
        if prev:
            last_t, last_bytes, last_ema = prev
            dt = now - last_t
            if dt > 0:
                inst = max(0.0, (done - last_bytes) / dt)
                ema = inst if last_ema <= 0 else last_ema * 0.6 + inst * 0.4
        self._rate[job.dest] = (now, done, ema)
        if ema <= 0:
            return -1, -1
        remaining = max(0, job.total_bytes - done)
        return int(remaining / ema), int(ema / 1000)   # seconds, KB/s (decimal)
