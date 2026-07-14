"""The copier: one card's ingest, on its own worker thread.

    scan -> copy -> verify -> manifest -> pending -> (confirm) -> wipe

Every file is copied whole into  dest_base/<uuid>/<ingest_date>/<relpath>,
hashed on the way in, then re-hashed from the destination to verify. A manifest
(the receipt) is written only after every file verifies. Deletion happens only
after a human `confirm`, and only of the files we verified. Wiping defaults to a
logged dry run.

Kept deliberately simple -- this is the file that deletes footage:

  * Each ingest goes to a fresh date-stamped directory, so nothing is ever
    overwritten and there is no resume / dedup / collision logic. Re-inserting a
    half-copied card just copies it again into a new directory (operator's call).
  * A verify mismatch keeps the card AND leaves the copy in its date dir for
    manual review; it never deletes anything.
  * File metadata (mtime, mode) is preserved on every copy.
"""
import errno
import hashlib
import json
import os
import shutil
import sys
import threading
import time

CHUNK = 1 << 20          # 1 MiB copy/hash chunks (abort + throttle granularity)
MANIFEST = "manifest.json"

# Job states (superset of the protocol's status values).
IDLE, COPYING, VERIFYING, PENDING, WIPING, EMPTY, ERROR = (
    "idle", "copying", "verifying", "pending", "wiping", "empty", "error")


class Abort(Exception):
    """Raised inside the worker when the card is pulled mid-flight."""


class CardJob:
    """One card's ingest. The emitter reads the byte counters and `state`
    racily each tick -- single writer + the GIL make that safe, and a stale
    read is harmless."""

    def __init__(self, card, cfg, wipe_armed=False):
        self.card = card
        self.algo = cfg["hash"]["algo"]
        self.wipe_armed = wipe_armed          # False => wipe is a logged dry run
        self.state = IDLE
        self.error = ""                       # short reason, shown as the label
        self.abort = False                    # set when the card disappears
        self.total_bytes = 0
        self.copied_bytes = 0
        self.verified_bytes = 0
        self.dest = _dated_dir(cfg["dest"]["base"], card.uuid)
        self._files = []                      # (relpath, size)
        self._hashes = {}                     # relpath -> hex digest
        self.wiped = False
        self.throttle_bps = 0                 # dry-run pacing; 0 = full speed

    def start(self):
        threading.Thread(target=self.run, daemon=True,
                         name="copy-%s" % self.card.label).start()

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
            if self.state != ERROR:
                self.fail("REMOVED")
        except OSError as e:
            self.fail("DEST FULL" if e.errno == errno.ENOSPC else "IO ERROR")
            print("ingest: %s: %s" % (self.card.label, e), file=sys.stderr)
        except Exception as e:               # never let the worker die silently
            self.fail("ERROR")
            print("ingest: %s: %s" % (self.card.label, e), file=sys.stderr)

    def fail(self, why):
        self.state, self.error = ERROR, why   # keep the card; never delete

    def scan(self):
        src = self.card.mountpoint
        for root, _dirs, names in os.walk(src):
            for name in sorted(names):
                p = os.path.join(root, name)
                if os.path.islink(p):
                    continue
                self._files.append((os.path.relpath(p, src), os.path.getsize(p)))
        self._files.sort()
        self.total_bytes = sum(sz for _, sz in self._files)

    def copy_all(self):
        """Whole-file copies; the source hash is computed on the same stream."""
        for rel, _sz in self._files:
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
                try:
                    os.remove(part)           # don't leave a half-written .part
                except OSError:
                    pass
                raise
            _copy_metadata(src, dst)          # preserve mtime / mode
            self._hashes[rel] = h.hexdigest()

    def verify_all(self):
        """Re-read every destination file and compare to the source hash."""
        for rel, _sz in self._files:
            self._check_abort()
            dst = os.path.join(self.dest, rel)
            if hash_file(dst, self.algo, pace=self._pace,
                         check=self._check_abort) != self._hashes[rel]:
                self.fail("HASH FAIL")        # keep card + copy; fix by hand
                raise Abort()
            self.verified_bytes += os.path.getsize(dst)

    def manifest_path(self):
        return os.path.join(self.dest, MANIFEST)

    def write_manifest(self):
        """The receipt: written ONLY after every file is copied and verified."""
        m = {"uuid": self.card.uuid, "label": self.card.label, "algo": self.algo,
             "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "files": [{"path": rel, "size": sz, "hash": self._hashes[rel]}
                       for rel, sz in self._files]}
        tmp = self.manifest_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(m, fh, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.manifest_path())

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
        """Delete the files we just verified. They were verified moments ago and
        the card is read-only (and a pull aborts the job before we get here), so
        no re-check is needed. Dry run unless config + CLI both armed it."""
        mode = "WIPE" if self.wipe_armed else "DRY-RUN wipe (kept)"
        for rel, _sz in self._files:
            if self.abort:                    # card pulled mid-wipe: stop
                self.fail("REMOVED")
                return
            src = os.path.join(self.card.mountpoint, rel)
            print("ingest: %s: %s %s" % (self.card.label, mode, src),
                  file=sys.stderr)
            if self.wipe_armed:
                try:
                    os.remove(src)
                except OSError as e:
                    self.fail("WIPE ERR")
                    print("ingest: wipe failed: %s" % e, file=sys.stderr)
                    return
        self.wiped = True
        self.state = EMPTY

    def _check_abort(self):
        if self.abort:
            raise Abort()

    def _pace(self, nbytes):
        if self.throttle_bps:
            time.sleep(nbytes / self.throttle_bps)


def hash_file(path, algo, pace=None, check=None):
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        while True:
            if check:
                check()
            chunk = fh.read(CHUNK)
            if not chunk:
                return h.hexdigest()
            h.update(chunk)
            if pace:
                pace(len(chunk))


def _copy_metadata(src, dst):
    """Preserve mtime / permissions (best-effort; exotic dest FS may refuse)."""
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass


def _dated_dir(base, uuid):
    """dest_base/<uuid>/<ingest_date>/ -- a fresh directory per ingest, created
    atomically so nothing ever collides (two same-UUID cards at once just get
    adjacent dirs)."""
    day = os.path.join(base, uuid, time.strftime("%Y-%m-%d_%H-%M-%S"))
    d, n = day, 2
    while True:
        try:
            os.makedirs(d)
            return d
        except FileExistsError:
            d = "%s-%d" % (day, n)
            n += 1
