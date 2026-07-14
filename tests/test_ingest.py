#!/usr/bin/env python3
"""Unit tests for the ingest daemon's copier + emitter (host/ingest.py).

Runs the real CardJob pipeline over a fake card tree in a temp dir and asserts
the locked rules: verify-before-manifest, resume, hash-mismatch keeps the
card, wipe only on confirm of a pending slot, dry-run wipe deletes nothing,
and the emitted slot lines fit the device parser's grammar. Stdlib only:

    python3 tests/test_ingest.py
"""
import io
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "host"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest

# What app/proto.c's sscanf accepts for a slot line (4 pairs mandatory).
SLOT_RE = re.compile(
    r"^slot (\d+) (-?\d+) (-?\d+) (idle|active|done|error|paused|pending)"
    r"( \d+ [0-9a-f]{1,6}){4} (.*)$")


def make_card(root, files):
    """A fake mounted card: {relpath: bytes} under root."""
    for rel, data in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)


class JobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "card")
        self.cfg = ingest.load_config(None)
        self.cfg["dest"]["base"] = os.path.join(self.tmp.name, "dest")
        self.files = {
            "DCIM/100/IMG_0001.JPG": b"a" * 3000,
            "DCIM/100/IMG_0002.JPG": b"b" * 5000,
            "note.txt": b"hello",
        }
        make_card(self.src, self.files)

    def tearDown(self):
        self.tmp.cleanup()

    def job(self, **kw):
        card = ingest.Card("mock-0", "TESTCARD", "UUID-01", self.src, 20000)
        return ingest.CardJob(card, self.cfg, **kw)

    def test_full_pipeline_to_pending(self):
        j = self.job()
        j.run()
        self.assertEqual(j.state, ingest.PENDING)
        self.assertEqual(j.total_bytes, 8005)
        self.assertEqual(j.copied_bytes, 8005)
        self.assertEqual(j.verified_bytes, 8005)
        # every file copied byte-identical
        for rel, data in self.files.items():
            with open(os.path.join(j.dest, rel), "rb") as fh:
                self.assertEqual(fh.read(), data)
        # manifest records all files with correct hashes
        with open(j.manifest_path()) as fh:
            m = json.load(fh)
        self.assertEqual(m["uuid"], "UUID-01")
        self.assertEqual({f["path"] for f in m["files"]}, set(self.files))
        import hashlib
        for f in m["files"]:
            self.assertEqual(
                f["hash"],
                hashlib.sha256(self.files[f["path"]]).hexdigest())

    def test_resume_skips_verified_files(self):
        j1 = self.job()
        j1.run()                       # first ingest writes the manifest
        j1.release()                   # card removed -> dest claim released
        j2 = self.job()                # same card comes back later
        j2.scan()
        self.assertEqual(len(j2._skipped), len(self.files))
        self.assertEqual(j2.verified_bytes, j2.total_bytes)

    def test_hash_mismatch_errors_and_keeps_everything(self):
        j = self.job()
        j.scan()
        j.copy_all()
        bad = os.path.join(j.dest, "note.txt")
        with open(bad, "wb") as fh:    # corrupt the copy before verification
            fh.write(b"HELLO")
        self.assertRaises(ingest.Abort, j.verify_all)
        self.assertEqual(j.state, ingest.ERROR)
        self.assertEqual(j.error, "HASH FAIL")
        self.assertFalse(os.path.exists(j.manifest_path()))  # no manifest
        for rel in self.files:         # source untouched
            self.assertTrue(os.path.exists(os.path.join(self.src, rel)))

    def test_no_manifest_until_verified(self):
        j = self.job()
        j.scan()
        j.copy_all()
        self.assertFalse(os.path.exists(j.manifest_path()))

    def test_confirm_refused_unless_pending(self):
        j = self.job()
        j.scan()
        j.state = ingest.COPYING
        self.assertFalse(j.request_wipe())
        self.assertEqual(j.state, ingest.COPYING)

    def test_dry_run_wipe_deletes_nothing(self):
        j = self.job()                 # wipe_armed defaults to False
        j.run()
        self.assertTrue(j.request_wipe())
        self._await_state(j, ingest.EMPTY)
        for rel in self.files:
            self.assertTrue(os.path.exists(os.path.join(self.src, rel)),
                            "dry-run wipe must not delete %s" % rel)

    def test_armed_wipe_deletes_only_manifest_files(self):
        j = self.job(wipe_armed=True)
        j.run()
        extra = os.path.join(self.src, "LATE.RAW")   # appears after verify
        with open(extra, "wb") as fh:
            fh.write(b"late")
        self.assertTrue(j.request_wipe())
        self._await_state(j, ingest.EMPTY)
        for rel in self.files:
            self.assertFalse(os.path.exists(os.path.join(self.src, rel)))
        self.assertTrue(os.path.exists(extra), "unverified file must survive")

    def test_duplicate_uuid_gets_suffixed_dest(self):
        j1, j2 = self.job(), self.job()
        self.assertNotEqual(j1.dest, j2.dest)
        self.assertTrue(j2.dest.endswith("UUID-01-2"))
        j1.release(), j2.release()

    def _await_state(self, job, state, timeout=5.0):
        import time
        deadline = time.monotonic() + timeout
        while job.state != state:
            self.assertLess(time.monotonic(), deadline,
                            "job stuck in %s" % job.state)
            time.sleep(0.01)


class EmitterTest(unittest.TestCase):
    def test_lines_fit_the_device_grammar(self):
        out = io.StringIO()
        em = ingest.Emitter(out, ingest.DEFAULTS["segments"])
        em.preamble()

        card = ingest.Card("mock-0", "A-VERY-LONG-CARD-LABEL-XYZ", "U", "/x",
                           10_000_000_000)
        job = ingest.CardJob.__new__(ingest.CardJob)   # no filesystem needed
        job.card, job.state, job.error = card, ingest.COPYING, ""
        job.wiped, job.wipe_armed, job.dest = False, False, "/dest/U"
        job.total_bytes = 9_000_000_000
        job.copied_bytes = 5_000_000_000
        job.verified_bytes = 2_000_000_000
        em.tick([job, None])
        lines = out.getvalue().splitlines()

        slots = [l for l in lines if l.startswith("slot ")]
        self.assertEqual(len(slots), 2)
        for l in slots:
            self.assertRegex(l, SLOT_RE)
        # permille of the card's own capacity: 2GB/1GB... relative scale
        m = SLOT_RE.match(slots[0])
        self.assertEqual(int(m.group(2)), 10_000)      # size_mb
        nums = [int(x) for x in slots[0].split()[5:12:2]]
        self.assertEqual(nums, [200, 300, 400, 0])     # up/cop/unc/unused
        self.assertLessEqual(sum(nums), 1000)
        label = m.group(6)
        self.assertLessEqual(len(label), 23)           # MAX_LABEL - 1
        # absent slot holds its column exactly like the mock does
        self.assertEqual(slots[1], "slot 1 -1 -1 idle 0 0 0 0 0 0 0 0 empty")
        self.assertIn("hb", lines)
        self.assertTrue(any(l.startswith("path 0 ") for l in lines))
        # preamble carries bg + numbers + a 4-row legend
        self.assertIn("bg 202020", lines)
        self.assertEqual(sum(l.startswith("legend ") for l in lines), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
