#!/usr/bin/env python3
"""Ingest daemon: discover cards behind the reader hub, copy + verify their
files, and drive the device line protocol (see ARCHITECTURE.md). This is the
real server that `host/mock_feed.py` stands in for.

    nix run .#ingest -- --dry-run | nix run .#sim      # no hardware needed
    nix run .#ingest -- --config host/ingest.toml      # the real thing

Per card (INGEST_PLAN.md, locked):

    idle -> copying -> verifying -> pending -confirm-> wiping -> empty
                |          |  (all files copied & hash-verified;
                +- error <-+   manifest written)

* Whole-file copy into dest_base/<partition-UUID>/, hash-verify every file
  (source stream vs a re-read of the destination), manifest only at the end.
* `confirm <i>` from the device (or typed on stdin) is the ONLY authorisation
  to wipe, and only a `pending` (fully verified + manifested) slot qualifies.
* Wiping defaults to a DRY RUN that logs what it would delete; real deletion
  needs `[wipe] enabled = true` in the config AND `--enable-wipe` on the CLI.

I/O plumbing: protocol lines go to stdout (pipe into the sim or at the board's
serial port); confirm lines are read from stdin. With `[serial] port` set (or
`--port`), the daemon opens that tty itself for both directions. Copy/verify
runs in one worker thread per card so the once-per-tick heartbeat never
starves, even mid-hash of a huge file (the device blanks after 2 s of
silence).

`--dry-run` swaps discovery for a scripted set of fake cards (real files in a
scratch dir, copied through the real copier at a throttled rate), so the whole
pipeline is testable on a machine with no readers.
"""
import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "serial": {"port": ""},              # "" = stdout/stdin (pipe mode)
    "hub": {"path_prefix": ""},          # /dev/disk/by-path prefix of the hub
    "dest": {"base": "/mnt/ingest"},     # files land in base/<uuid>/
    "hash": {"algo": "sha256"},
    "segments": {
        # Okabe-Ito-ish, colourblind-safe; meanings are the host's to assign.
        "uploaded": "#22C35E",   # copied AND verified (manifest-backed)
        "copied": "#0072B2",     # copied, not yet verified
        "uncopied": "#E69F00",   # still only on the card
        "empty": "#202020",      # free space on the card (the `bg` colour)
        "numbers": True,
    },
    "confirm": {"method": "stdin"},      # stdin | serial (same parser either way)
    "poll": {"interval_ms": 500},
    "wipe": {"enabled": False},          # real deletion also needs --enable-wipe
}


def load_config(path):
    """DEFAULTS overlaid with the TOML file (one level deep, like the tables)."""
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if path:
        import tomllib  # stdlib in Python >= 3.11 (flake pins 3.12)
        with open(path, "rb") as fh:
            user = tomllib.load(fh)
        for section, values in user.items():
            cfg.setdefault(section, {}).update(values)
    return cfg


def color(s):
    """'#RRGGBB' / 'RRGGBB' / int -> int, for '%06x' protocol fields."""
    if isinstance(s, int):
        return s & 0xFFFFFF            # already a number; don't re-parse as hex
    return int(str(s).lstrip("#"), 16) & 0xFFFFFF


def as_bool(v):
    """Strict truthiness for config values: a TOML bool stays itself, but a
    quoted string like "false"/"0"/"no" must NOT read as True (plain Python
    truthiness treats any non-empty string as True)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


# --------------------------------------------------------------------------- #
# Discovery: which slots exist, and what card (if any) is in each
# --------------------------------------------------------------------------- #
# A "slot" is a fixed physical position (reader LUN); its index never moves
# while the daemon runs, so bars hold their place on the display. A slot is
# occupied when its block device reports non-zero size (media present).

# slots() yields one of: a Card (media present), None (definitely absent), or
# UNKNOWN (probe hit a transient error — leave the slot's job untouched rather
# than falsely declaring the card removed mid-copy).
UNKNOWN = object()


class Card:
    """A present card: identity + where to read it from."""

    def __init__(self, ident, label, uuid, mountpoint, capacity_bytes):
        self.ident = ident                # stable slot identity (by-path / mock)
        self.label = label                # short name for the display column
        self.uuid = uuid                  # partition UUID -> dest sub-dir
        self.mountpoint = mountpoint      # where its files are readable
        self.capacity_bytes = capacity_bytes


class HubDiscovery:
    """Real readers: /dev/disk/by-path entries under the configured hub prefix.

    The by-path string encodes USB topology, so sorting it gives a stable
    physical order (INGEST_PLAN.md); LUN0/LUN1 are a reader's two slots. The
    /dev/sdX letters are never trusted.
    """

    BY_PATH = "/dev/disk/by-path"

    def __init__(self, path_prefix):
        self.prefix = path_prefix
        self.slot_ids = self._enumerate()  # fixed for the daemon's lifetime

    def _entries(self):
        try:
            names = os.listdir(self.BY_PATH)
        except FileNotFoundError:
            return []
        return sorted(n for n in names
                      if n.startswith(self.prefix) and "-part" not in n)

    def _enumerate(self):
        ids = self._entries()
        if not ids:
            print("warning: no block devices under hub prefix %r in %s"
                  % (self.prefix, self.BY_PATH), file=sys.stderr)
        return ids

    def slots(self):
        """-> list of Card-or-None, one per fixed slot, in physical order."""
        out = []
        for ident in self.slot_ids:
            dev = os.path.realpath(os.path.join(self.BY_PATH, ident))
            out.append(self._probe(ident, dev))
        return out

    def _probe(self, ident, dev):
        node = os.path.basename(dev)
        try:
            with open("/sys/class/block/%s/size" % node) as fh:
                sectors = int(fh.read())
        except (OSError, ValueError):
            return UNKNOWN                    # transient read error, not removal
        if sectors == 0:
            return None                       # reader present, no media
        cap = sectors * 512
        part, uuid = self._partition(dev)
        mnt = self._mountpoint(part or dev)
        label = self._fslabel(part or dev) or (uuid or node)[:12]
        return Card(ident, label, uuid or node, mnt, cap)

    @staticmethod
    def _partition(dev):
        """First partition of dev (or dev itself) + its UUID via by-uuid."""
        node = os.path.basename(dev)
        parts = sorted(p for p in os.listdir("/sys/class/block/%s" % node)
                       if p.startswith(node))
        part = "/dev/" + parts[0] if parts else dev
        return part, _reverse_symlink("/dev/disk/by-uuid", part)

    @staticmethod
    def _fslabel(dev):
        return _reverse_symlink("/dev/disk/by-label", dev)

    @staticmethod
    def _mountpoint(dev):
        real = os.path.realpath(dev)
        try:
            with open("/proc/mounts") as fh:
                for line in fh:
                    fields = line.split()
                    if os.path.realpath(fields[0]) == real:
                        return fields[1].replace("\\040", " ")
        except OSError:
            pass
        return None                           # present but not mounted


def _reverse_symlink(dirpath, dev):
    """Find the name in dirpath whose symlink resolves to dev (else None)."""
    real = os.path.realpath(dev)
    try:
        for name in os.listdir(dirpath):
            if os.path.realpath(os.path.join(dirpath, name)) == real:
                return name
    except OSError:
        pass
    return None


class MockDiscovery:
    """--dry-run: scripted fake cards, real files in a scratch dir.

    Each spec is (name, n_files, kib_per_file, insert_after_s, remove_after_s);
    remove_after_s = None means the card stays in. Capacity is padded ~25%
    above the used bytes so the bars visibly fill most of the column.
    """

    SPECS = [
        ("SANDISK64", 6, 2048, 0.0, None),
        ("EXTREME32", 4, 1536, 0.0, None),
        ("LEXAR64",   5, 1024, 2.0, None),
        (None,        0, 0,    0.0, None),    # a slot that stays empty
        ("PNY256",    3, 768,  4.0, None),
    ]

    def __init__(self, root):
        self.root = root
        self.t0 = time.monotonic()
        self.cards = []
        for i, (name, nfiles, kib, ins, rm) in enumerate(self.SPECS):
            card = None
            if name:
                src = os.path.join(root, "card%d" % i)
                used = _make_fake_card(src, nfiles, kib, seed=i)
                card = Card("mock-%d" % i, name, "MOCK-%04d" % (0x1000 + i),
                            src, int(used * 1.25))
            self.cards.append((card, ins, rm))

    def slots(self):
        t = time.monotonic() - self.t0
        return [card if card and ins <= t and (rm is None or t < rm) else None
                for card, ins, rm in self.cards]


def _make_fake_card(root, nfiles, kib, seed):
    """Deterministic fake DCIM tree; returns total bytes written."""
    d = os.path.join(root, "DCIM", "100MOCK")
    os.makedirs(d, exist_ok=True)
    total = 0
    for n in range(nfiles):
        blob = hashlib.sha256(b"%d:%d" % (seed, n)).digest() * (kib * 1024 // 32)
        with open(os.path.join(d, "IMG_%04d.JPG" % n), "wb") as fh:
            fh.write(blob)
        total += len(blob)
    return total


# --------------------------------------------------------------------------- #
# Copier: per-card worker (copy -> verify -> manifest -> pending -> wipe)
# --------------------------------------------------------------------------- #

CHUNK = 1 << 20          # 1 MiB copy/hash chunks (abort + throttle granularity)
MANIFEST = "manifest.json"

# Job states (superset of the protocol's status values).
IDLE, COPYING, VERIFYING, PENDING, WIPING, EMPTY, ERROR = (
    "idle", "copying", "verifying", "pending", "wiping", "empty", "error")

_claimed_dests = set()   # dest dirs owned by live jobs (duplicate-UUID guard)
_claim_lock = threading.Lock()


class CardJob:
    """One card's ingest, run on its own thread. The emitter reads the byte
    counters and `state` racily each tick — single writers + the GIL keep that
    safe, and a stale tick is harmless."""

    def __init__(self, card, cfg, wipe_armed=False):
        self.card = card
        self.algo = cfg["hash"]["algo"]
        self.wipe_armed = wipe_armed          # False => wipe is a logged dry run
        self.state = IDLE
        self.error = ""                       # short reason, shown as the label
        self.abort = False                    # set when the card disappears
        self.total_bytes = 0                  # used bytes on the card
        self.copied_bytes = 0                 # streamed to dest (incl. verified)
        self.verified_bytes = 0               # dest re-read + hash matched
        self.dest = self._claim_dest(cfg["dest"]["base"], card.uuid)
        self._files = []                      # (relpath, size)
        self._hashes = {}                     # relpath -> source hex digest
        self._skipped = set()                 # relpaths resumed from a manifest
        self.wiped = False                    # a confirm-triggered wipe ran
        self.throttle_bps = 0                 # dry-run pacing; 0 = full speed
        self._run_done = threading.Event()    # set when the copy worker exits

    @staticmethod
    def _claim_dest(base, uuid):
        """dest_base/<uuid>/, suffixed if another live card claims the UUID."""
        with _claim_lock:
            dest, n = os.path.join(base, uuid), 2
            while dest in _claimed_dests:
                dest = os.path.join(base, "%s-%d" % (uuid, n))
                n += 1
            _claimed_dests.add(dest)
            return dest

    def release(self):
        with _claim_lock:
            _claimed_dests.discard(self.dest)

    def start(self):
        threading.Thread(target=self.run, daemon=True,
                         name="copy-%s" % self.card.label).start()

    # ---- pipeline ---------------------------------------------------------

    def run(self):
        try:
            self.scan()
            if self.total_bytes == 0:
                self.state = EMPTY            # nothing on the card
                return
            self.state = COPYING
            self.copy_all()
            self.state = VERIFYING
            self.verify_all()
            self.write_manifest()
            self.state = PENDING              # wait for a human's confirm
        except Abort:
            if self.state != ERROR:           # verify_all may have failed first
                self.fail("REMOVED")
        except OSError as e:
            import errno
            self.fail("DEST FULL" if e.errno == errno.ENOSPC else "IO ERROR")
            print("ingest: %s: %s" % (self.card.label, e), file=sys.stderr)
        except Exception as e:                # never let the worker die silently
            self.fail("ERROR")
            print("ingest: %s: worker error: %s"
                  % (self.card.label, e), file=sys.stderr)
        finally:
            self._run_done.set()              # dest is safe to release once set

    def fail(self, why):
        self.state, self.error = ERROR, why   # keep the card; never delete

    def scan(self):
        """List the card's files + load a prior manifest for resume."""
        src = self.card.mountpoint
        for root, _dirs, names in os.walk(src):
            for name in sorted(names):
                p = os.path.join(root, name)
                if os.path.islink(p):
                    continue
                rel = os.path.relpath(p, src)
                self._files.append((rel, os.path.getsize(p)))
        self._files.sort()
        self.total_bytes = sum(sz for _, sz in self._files)

        old = self.read_manifest()
        for rel, sz in self._files:
            dst = os.path.join(self.dest, rel)
            if not (rel in old and old[rel]["size"] == sz
                    and os.path.isfile(dst) and os.path.getsize(dst) == sz):
                continue
            # Size + manifest match is not enough to skip: re-hash the existing
            # destination and only trust it if the bytes still match. Otherwise
            # a silently-corrupted (but same-length) copy would be treated as
            # verified and could authorise wiping the intact source.
            try:
                got = hash_file(dst, self.algo, check=self._check_abort)
            except OSError:
                continue                      # unreadable -> just re-copy it
            if got != old[rel]["hash"]:
                continue                      # corrupt/stale -> re-copy it
            self._skipped.add(rel)            # verified in an earlier run
            self._hashes[rel] = old[rel]["hash"]
            self.copied_bytes += sz
            self.verified_bytes += sz

    def copy_all(self):
        """Whole-file copies; the source hash is computed on the same stream."""
        dirs = set()
        for rel, _sz in self._files:
            if rel in self._skipped:
                continue
            self._check_abort()
            src = os.path.join(self.card.mountpoint, rel)
            dst = os.path.join(self.dest, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            part = dst + ".part"
            h = hashlib.new(self.algo)
            try:
                with open(src, "rb") as fi, open(part, "wb") as fo:
                    while True:
                        self._check_abort()
                        chunk = fi.read(CHUNK)
                        if not chunk:
                            break
                        h.update(chunk)
                        fo.write(chunk)
                        self.copied_bytes += len(chunk)
                        self._pace(len(chunk))
                    fo.flush()
                    os.fsync(fo.fileno())
                os.replace(part, dst)         # never leave torn files visible
            except BaseException:
                # abort / IO error: don't leave a half-written .part behind
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise
            self._hashes[rel] = h.hexdigest()
            dirs.add(os.path.dirname(dst))
        for d in dirs:                        # make the renames durable
            _fsync_dir(d)

    def verify_all(self):
        """Re-read every destination file and compare to the source hash."""
        for rel, _sz in self._files:
            if rel in self._skipped:
                continue
            self._check_abort()
            got = hash_file(os.path.join(self.dest, rel), self.algo,
                            pace=self._pace, check=self._check_abort,
                            drop_cache=True)   # read from media, not page cache
            if got != self._hashes[rel]:
                self.fail("HASH FAIL")        # keep card + copy; human decides
                raise Abort()
            self.verified_bytes += os.path.getsize(os.path.join(self.dest, rel))

    # ---- manifest (the record of a verified ingest) ------------------------

    def manifest_path(self):
        return os.path.join(self.dest, MANIFEST)

    def read_manifest(self):
        try:
            with open(self.manifest_path()) as fh:
                m = json.load(fh)
            if m.get("algo") != self.algo:
                return {}
            # only keep well-formed entries; a foreign/hand-edited manifest with
            # a missing key must not KeyError later in scan() and kill the worker
            return {f["path"]: f for f in m.get("files", [])
                    if isinstance(f, dict) and {"path", "size", "hash"} <= f.keys()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def write_manifest(self):
        """Written ONLY after every file is copied and hash-verified."""
        m = {"uuid": self.card.uuid, "label": self.card.label,
             "algo": self.algo,
             "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "files": [{"path": rel, "size": sz, "hash": self._hashes[rel]}
                       for rel, sz in self._files]}
        tmp = self.manifest_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(m, fh, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())             # manifest data durable before wipe
        os.replace(tmp, self.manifest_path())
        _fsync_dir(self.dest)                 # and its rename durable too

    # ---- wipe (only ever entered via a confirm on a PENDING job) -----------

    def request_wipe(self):
        """Handle `confirm <i>`. Refuses anything not fully verified."""
        if self.state != PENDING:
            print("ingest: confirm for %s ignored (state=%s, not pending)"
                  % (self.card.label, self.state), file=sys.stderr)
            return False
        self.state = WIPING
        threading.Thread(target=self._wipe, daemon=True,
                         name="wipe-%s" % self.card.label).start()
        return True

    def _wipe(self):
        """Delete ONLY files listed in the just-written manifest, and ONLY after
        re-confirming each source still byte-matches the copy we verified — so a
        file changed after verification is never deleted. Dry run unless config
        + CLI both armed it."""
        mode = "WIPE" if self.wipe_armed else "DRY-RUN wipe (kept)"
        deleted = failed = 0
        for rel, _sz in self._files:
            if self.abort:                    # card pulled mid-wipe: stop now
                print("ingest: %s: wipe aborted (card removed)"
                      % self.card.label, file=sys.stderr)
                self.fail("REMOVED")
                return
            src = os.path.join(self.card.mountpoint, rel)
            print("ingest: %s: %s %s" % (self.card.label, mode, src),
                  file=sys.stderr)
            if not self.wipe_armed:
                continue
            # TOCTOU guard: re-hash the source and refuse to delete unless it
            # still matches the hash whose copy we verified. Anything else
            # (changed in place, unreadable) keeps the whole card, human decides.
            try:
                now = hash_file(src, self.algo, check=self._check_abort)
            except Abort:
                self.fail("REMOVED")
                return
            except OSError:
                now = None
            if now != self._hashes.get(rel):
                print("ingest: %s: SOURCE CHANGED since verify, aborting wipe "
                      "(%s)" % (self.card.label, src), file=sys.stderr)
                self.fail("SRC CHANGED")
                return
            try:
                os.remove(src)
                deleted += 1
            except OSError as e:
                failed += 1
                print("ingest: %s: wipe failed on %s: %s"
                      % (self.card.label, src, e), file=sys.stderr)
        if failed:
            self.fail("WIPE ERR")
            print("ingest: %s: wiped %d/%d files, %d failed"
                  % (self.card.label, deleted, len(self._files), failed),
                  file=sys.stderr)
            return
        if self.wipe_armed:
            self._prune_dirs()
        self.wiped = True
        self.state = EMPTY

    def _prune_dirs(self):
        """rmdir only the (now-empty) directories that held manifest files —
        never arbitrary pre-existing empty directories under the mountpoint."""
        mnt = os.path.realpath(self.card.mountpoint)
        dirs = set()
        for rel, _sz in self._files:
            d = os.path.dirname(os.path.join(self.card.mountpoint, rel))
            while True:
                rd = os.path.realpath(d)
                if rd == mnt or not rd.startswith(mnt + os.sep):
                    break
                dirs.add(d)
                d = os.path.dirname(d)
        for d in sorted(dirs, key=len, reverse=True):   # deepest first
            try:
                os.rmdir(d)                             # only succeeds if empty
            except OSError:
                pass

    # ---- worker plumbing ----------------------------------------------------

    def _check_abort(self):
        if self.abort:
            raise Abort()

    def _pace(self, nbytes):
        if self.throttle_bps:
            time.sleep(nbytes / self.throttle_bps)


class Abort(Exception):
    pass


def hash_file(path, algo, pace=None, check=None, drop_cache=False):
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        if drop_cache:
            # evict this file from the page cache so the read below actually
            # comes off the destination media (Linux; best-effort elsewhere).
            try:
                os.fsync(fh.fileno())
                os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass
        while True:
            if check:
                check()
            chunk = fh.read(CHUNK)
            if not chunk:
                return h.hexdigest()
            h.update(chunk)
            if pace:
                pace(len(chunk))


def _fsync_dir(path):
    """Durably persist a directory's entries (renames) — best-effort."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Emitter: jobs -> protocol lines, once per tick
# --------------------------------------------------------------------------- #

class Emitter:
    """Formats the model for the device. Segments are permille of the card's
    OWN capacity (relative scale, like the mock): uploaded / copied-not-yet-
    verified / uncopied stack from the bottom; the leftover is free space in
    the `bg` colour."""

    def __init__(self, out, seg_cfg):
        self.out = out
        self.up = color(seg_cfg["uploaded"])
        self.cop = color(seg_cfg["copied"])
        self.unc = color(seg_cfg["uncopied"])
        self.bg = color(seg_cfg["empty"])
        self.numbers = 1 if as_bool(seg_cfg.get("numbers", True)) else 0
        self._rate = {}                        # slot -> (t, done_bytes, ewma bps)
        self._paths = {}                       # last `path` sent per slot
        self._last_warn = 0.0                  # rate-limit link-hiccup warnings

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
        skips interpreter shutdown, which would try to flush dead stdout."""
        os._exit(0)

    def _link_hiccup(self, e):
        """A non-fatal write error (e.g. a serial glitch on a real link): warn,
        rate-limited, and skip this frame rather than crash mid-ingest."""
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
            # An absent reader and a wiped slot are the same blank row: a wiped
            # card holds no data, so it drops straight to empty with no flash.
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
            EMPTY:     ("idle", "empty"),      # tick() renders EMPTY as a blank row
            ERROR:     ("error", job.error or "ERROR"),
        }[job.state]
        eta = self._eta(i, job)
        return ("slot %d %d %d %s %d %06x %d %06x %d %06x 0 0 %s"
                % (i, size_mb, eta, status, up, self.up, cop, self.cop,
                   unc, self.unc, label[:23]))

    def _eta(self, i, job):
        """Seconds left, from an EWMA of copy+verify progress. -1 = unknown."""
        if job.state not in (COPYING, VERIFYING):
            self._rate.pop(i, None)
            return -1
        done = job.copied_bytes + job.verified_bytes    # 2x total when finished
        now = time.monotonic()
        prev = self._rate.get(i)
        bps = prev[2] if prev else 0.0
        if prev and now > prev[0]:
            inst = (done - prev[1]) / (now - prev[0])
            bps = inst if not bps else 0.7 * bps + 0.3 * inst
        self._rate[i] = (now, done, bps)
        if bps <= 0:
            return -1
        return int((2 * job.total_bytes - done) / bps) + 1


# --------------------------------------------------------------------------- #
# Confirm channel: `confirm <i>` lines from the device (or a human on stdin)
# --------------------------------------------------------------------------- #

def confirm_reader(stream, q, reopen=None):
    """Thread: parse `confirm <i>` lines into a queue of slot indices. When a
    reopen() is given (a serial link), a read error / EOF reconnects instead of
    silently killing the wipe-authorisation channel; on stdin, EOF just ends."""
    while True:
        try:
            for raw in stream:
                if isinstance(raw, bytes):
                    raw = raw.decode("ascii", "replace")
                m = re.match(r"\s*confirm\s+(\d+)\s*$", raw)
                if m:
                    q.put(int(m.group(1)))
        except OSError as e:
            print("ingest: confirm read error: %s" % e, file=sys.stderr)
        if reopen is None:
            return                             # stdin/pipe closed: nothing more
        try:
            stream = reopen()                  # serial disconnect: reconnect
        except OSError as e:
            print("ingest: confirm reopen failed: %s" % e, file=sys.stderr)
            time.sleep(1)


def open_serial(port):
    """Open a tty read/write, raw, stdlib-only (USB CDC ignores the baud)."""
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
    try:
        import termios
        attrs = termios.tcgetattr(fd)
        termios.cfmakeraw(attrs)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except (ImportError, OSError):
        pass                                   # plain file (tests) — fine
    return os.fdopen(fd, "rb", buffering=0), os.fdopen(os.dup(fd), "w")


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", help="TOML config (see host/ingest.toml)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fake cards in a scratch dir; no hardware, no serial")
    ap.add_argument("--port", help="serial device (overrides [serial] port)")
    ap.add_argument("--dest", help="override [dest] base")
    ap.add_argument("--hub-prefix", help="override [hub] path_prefix")
    ap.add_argument("--interval-ms", type=int, help="override [poll] interval_ms")
    ap.add_argument("--ticks", type=int, default=0,
                    help="exit after N ticks (0 = run forever); for tests")
    ap.add_argument("--auto-confirm", type=float, default=0, metavar="S",
                    help="[dry-run only] auto-confirm a pending slot after S s")
    ap.add_argument("--enable-wipe", action="store_true",
                    help="really delete on confirm (also needs [wipe] enabled)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.interval_ms is not None:           # honour an explicit 0, too
        cfg["poll"]["interval_ms"] = args.interval_ms

    # Real deletion needs BOTH the config flag and the CLI flag; and never in
    # a dry run (the "cards" there are scratch files, but stay consistent).
    # as_bool() so a quoted `enabled = "false"` can't read as True.
    wipe_armed = (as_bool(cfg["wipe"].get("enabled", False)) and args.enable_wipe
                  and not args.dry_run)
    if args.enable_wipe and not wipe_armed:
        print("ingest: --enable-wipe ignored ([wipe] enabled is false%s)"
              % (" / dry-run" if args.dry_run else ""), file=sys.stderr)

    if args.dry_run:
        root = tempfile.mkdtemp(prefix="ingest-dry-")
        disco = MockDiscovery(root)
        dest_base = args.dest or os.path.join(root, "dest")
        print("ingest: dry run; cards + dest under %s" % root, file=sys.stderr)
    else:
        disco = HubDiscovery(args.hub_prefix or cfg["hub"]["path_prefix"])
        dest_base = args.dest or cfg["dest"]["base"]
    cfg["dest"]["base"] = dest_base

    # Wire up the display + confirm channel.
    port = args.port or cfg["serial"]["port"]
    if port and not args.dry_run:
        rx, tx = open_serial(port)
        def reopen_rx():                       # reconnect just the read side
            r, t = open_serial(port)
            t.close()
            return r
    else:
        rx, tx = sys.stdin, sys.stdout
        reopen_rx = None
    emitter = Emitter(tx, cfg["segments"])
    confirms = queue.Queue()
    threading.Thread(target=confirm_reader, args=(rx, confirms),
                     kwargs={"reopen": reopen_rx}, daemon=True).start()

    emitter.preamble()
    jobs = {}                                  # slot index -> CardJob
    retiring = []                              # removed jobs awaiting worker exit
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
                job.abort = True               # stop its worker
                if job.state in (IDLE, COPYING, VERIFYING, WIPING):
                    job.fail("REMOVED")        # yanked mid-flight
                retiring.append(job)           # release dest once its worker stops
                del jobs[i]
                job = None
            if card is not None and job is None:
                if card.mountpoint is None:
                    continue                   # present but unreadable; skip
                job = CardJob(card, cfg, wipe_armed=wipe_armed)
                if args.dry_run:
                    job.throttle_bps = 1_500_000  # visible progress in the sim
                jobs[i] = job
                job.start()

        # Release a removed job's dest claim only once its copy worker has
        # actually stopped, so a duplicate-UUID card can't re-claim and write a
        # directory another thread is still using.
        keep = []
        for j in retiring:
            if j._run_done.is_set():
                j.release()
            else:
                keep.append(j)
        retiring = keep

        # Wipe confirmations (the only path to deletion).
        try:
            while True:
                i = confirms.get_nowait()
                if i in jobs:
                    jobs[i].request_wipe()
                else:
                    print("ingest: confirm %d ignored (no card)" % i,
                          file=sys.stderr)
        except queue.Empty:
            pass
        if args.dry_run and args.auto_confirm:
            now = time.monotonic()
            for i, job in jobs.items():
                if job.state == PENDING:
                    since = pending_since.setdefault(i, now)
                    if now - since >= args.auto_confirm:
                        job.request_wipe()
                else:
                    pending_since.pop(i, None)

        # One display frame. Absent slots keep their column (like the mock).
        n = max(len(slots), (max(jobs) + 1) if jobs else 0)
        emitter.tick([jobs.get(i) for i in range(n)])

        tick += 1
        if args.ticks and tick >= args.ticks:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
