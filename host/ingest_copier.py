"""The copier: one card's ingest, on its own worker thread.

    scan -> rclone copy -> rclone check -> manifest -> pending -> (confirm) -> wipe

The heavy lifting is delegated to **rclone** (thoroughly tested): `rclone copy`
transfers every file under the card into dest_base/<uuid>/<ingest_date>/ and
verifies each transfer, then `rclone check --one-way` re-reads the source and
destination *independently* and compares hashes -- that independent double read
is what makes the verify meaningful for a system that then deletes the card.
The receipt is a plain `<ALGO>SUMS` file (portable; re-checkable with `sha1sum
-c`), and the hash (sha1) is the one the cloud remotes serve from metadata.

Kept deliberately small -- this is the file that deletes footage:

  * Each ingest goes to a fresh date-stamped dir, so nothing is ever overwritten
    and there is no resume/dedup/collision logic. A replug just copies again.
  * A verify mismatch keeps the card AND its copy for manual review; never deletes.
  * The wipe is confirm-gated, dry-run by default, deletes only the files we
    scanned, and re-checks each source (size+mtime) right before deleting.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

# The receipt filename is derived from the hash algo (e.g. SHA1SUMS). Format is
# `<hash>  <path>` per line -- `sha1sum -c`-able.
def manifest_name(algo):
    return algo.upper() + "SUMS"

# Two single-writer state files per ingest dir, so no file is co-owned:
#   metadata.json  -- the copier's immutable receipt (present == verified)
#   uploaded.json  -- the uploader's state  (present == uploaded)
# "ready to upload" = has metadata.json, no uploaded.json.
METADATA = "metadata.json"
UPLOADED = "uploaded.json"


def _read_json(d, name):
    try:
        with open(os.path.join(d, name)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_json(d, name, obj):
    tmp = os.path.join(d, name + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, os.path.join(d, name))       # atomic; readers never tear


def read_metadata(d):
    return _read_json(d, METADATA)


def read_uploaded(d):
    return _read_json(d, UPLOADED)


def write_metadata(d, meta):
    _write_json(d, METADATA, meta)


def write_uploaded(d, meta):
    _write_json(d, UPLOADED, meta)

# Job states (superset of the protocol's status values).
IDLE, COPYING, VERIFYING, PENDING, WIPING, EMPTY, ERROR = (
    "idle", "copying", "verifying", "pending", "wiping", "empty", "error")


class Abort(Exception):
    """Raised inside the worker when the card is pulled mid-flight."""


class CardJob:
    """One card's ingest. The emitter reads the byte counters and `state`
    racily each tick -- single writer + the GIL make that safe, and a stale
    read is harmless."""

    def __init__(self, card, cfg, wipe_armed=False, throttle_bps=0):
        self.card = card
        self.algo = cfg["hash"]["algo"]
        self.wipe_armed = wipe_armed          # False => wipe is a logged dry run
        self.throttle_bps = throttle_bps      # dry-run pacing (rclone --bwlimit)
        self.state = IDLE
        self.error = ""                       # short reason, shown as the label
        self.abort = False                    # set when the card disappears
        self.total_bytes = 0
        self.copied_bytes = 0
        self.verified_bytes = 0
        self.uploaded_bytes = 0               # filled from metadata.json (uploader)
        self.dest = _dated_dir(cfg["dest"]["base"], card.uuid)
        self._files = []                      # (relpath, size)
        self._src_meta = {}                   # relpath -> (size, mtime_ns) at scan
        self.wiped = False

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
            self.copy()
            self.state = VERIFYING
            self.verify()
            self.write_manifest()
            self.verified_bytes = self.total_bytes
            write_metadata(self.dest, {   # the receipt; never rewritten
                "uuid": self.card.uuid, "label": self.card.label,
                "ingest_date": os.path.basename(self.dest), "algo": self.algo,
                "files": len(self._files), "total_bytes": self.total_bytes,
                "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })
            self.state = PENDING              # wait for a human's confirm
        except Abort:
            if self.state != ERROR:
                self.fail("REMOVED")
        except Exception as e:               # never let the worker die silently
            self.fail("ERROR")
            print("ingest: %s: %s" % (self.card.label, e), file=sys.stderr)

    def fail(self, why):
        self.state, self.error = ERROR, why   # keep the card; never delete

    def scan(self):
        """List the card's files (for the wipe + progress); record size+mtime."""
        src = self.card.mountpoint
        for root, _dirs, names in os.walk(src):
            for name in sorted(names):
                p = os.path.join(root, name)
                if os.path.islink(p):
                    continue
                rel = os.path.relpath(p, src)
                st = os.stat(p)
                self._files.append((rel, st.st_size))
                self._src_meta[rel] = (st.st_size, st.st_mtime_ns)
        self._files.sort()
        self.total_bytes = sum(sz for _, sz in self._files)

    # ---- rclone-backed copy + verify --------------------------------------

    def copy(self):
        """rclone copy the whole card; live byte count drives the display."""
        args = ["copy", self.card.mountpoint, self.dest,
                "--transfers", "4", "--checkers", "8"]
        if self.throttle_bps:                 # dry-run: throttle for a visible bar
            args += ["--bwlimit", "%dk" % max(1, self.throttle_bps // 1024)]
        rc = self._rclone(args, on_bytes=lambda b: setattr(self, "copied_bytes", b))
        if rc != 0:
            self.fail("COPY ERR")
            raise Abort()
        self.copied_bytes = self.total_bytes

    def verify(self):
        """rclone check --one-way: independently re-hash source vs dest."""
        rc = self._rclone(["check", self.card.mountpoint, self.dest, "--one-way"])
        if rc != 0:
            self.fail("HASH FAIL")            # keep card + copy; fix by hand
            raise Abort()

    def write_manifest(self):
        """The receipt: `<algo>` hashes of every verified file (md5sum -c-able).
        Capture stdout ourselves (portable across rclone versions) into a temp
        file OUTSIDE dest -- so the hashsum can't include its own output -- then
        move it in atomically."""
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.dest))
        try:
            with os.fdopen(fd, "w") as fo:
                # uppercase hash name works on old + new rclone (1.53 .. 1.74)
                # `rclone <algo>sum` (md5sum/sha1sum) is stable across rclone
                # versions, unlike `hashsum <name>` whose hash names changed.
                rc = subprocess.run(
                    ["rclone", self.algo + "sum", self.dest],
                    stdout=fo, stderr=subprocess.DEVNULL).returncode
            if rc != 0:
                raise RuntimeError("rclone hashsum failed (%d)" % rc)
            os.replace(tmp, self.manifest_path())
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def manifest_path(self):
        return os.path.join(self.dest, manifest_name(self.algo))

    def _rclone(self, args, on_bytes=None):
        """Run rclone, forwarding abort. With on_bytes, parse --stats JSON for a
        live transferred-byte count. Returns the exit code."""
        if on_bytes is not None:
            args = args + ["--use-json-log", "--stats", "500ms",
                           "--stats-log-level", "NOTICE"]
        p = subprocess.Popen(["rclone"] + args, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True)
        try:
            for line in p.stderr:
                if self.abort:
                    p.terminate()
                    raise Abort()
                if on_bytes is not None:
                    b = _stats_bytes(line)
                    if b is not None:
                        on_bytes(b)
            return p.wait()
        finally:
            if p.poll() is None:
                p.terminate()
                p.wait()

    # ---- wipe (only ever entered via a confirm on a PENDING job) -----------

    def request_wipe(self):
        if self.state != PENDING:
            print("ingest: confirm for %s ignored (state=%s, not pending)"
                  % (self.card.label, self.state), file=sys.stderr)
            return False
        self.state = WIPING
        threading.Thread(target=self._wipe, daemon=True,
                         name="wipe-%s" % self.card.label).start()
        return True

    def _wipe(self):
        """Delete the files we verified. Before each delete, cheaply confirm the
        source is unchanged since scan (size + mtime -- the card is read-only, so
        a change means don't touch it). Dry run unless config + env armed it."""
        mode = "WIPE" if self.wipe_armed else "DRY-RUN wipe (kept)"
        for rel, _sz in self._files:
            if self.abort:                    # card pulled mid-wipe: stop
                self.fail("REMOVED")
                return
            src = os.path.join(self.card.mountpoint, rel)
            print("ingest: %s: %s %s" % (self.card.label, mode, src),
                  file=sys.stderr)
            if not self.wipe_armed:
                continue
            if not self._unchanged(src, rel):
                self.fail("SRC CHANGED")      # touched since scan; keep the card
                print("ingest: %s: source changed, not deleting %s"
                      % (self.card.label, src), file=sys.stderr)
                return
            try:
                os.remove(src)
            except OSError as e:
                self.fail("WIPE ERR")
                print("ingest: wipe failed: %s" % e, file=sys.stderr)
                return
        self.wiped = True
        self.state = EMPTY

    def _unchanged(self, src, rel):
        """Source still matches what we scanned (size + mtime)?"""
        try:
            st = os.stat(src)
        except OSError:
            return False
        return (st.st_size, st.st_mtime_ns) == self._src_meta.get(rel)


def _stats_bytes(line):
    """Pull stats.bytes out of an rclone --use-json-log line (or None)."""
    try:
        st = json.loads(line).get("stats")
    except ValueError:
        return None
    return st.get("bytes") if st else None


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
