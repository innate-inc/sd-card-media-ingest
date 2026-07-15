#!/usr/bin/env python3
"""Unit tests for the ingest daemon's copier + emitter.

Runs the real CardJob pipeline over a fake card tree in a temp dir and asserts
the locked rules: fresh dated dest dir, metadata preserved, verify-before-
manifest, hash-mismatch keeps the card, wipe only on confirm of a pending slot,
dry-run wipe deletes nothing, and the emitted slot lines fit the device parser's
grammar. Stdlib only:

    python3 tests/test_ingest.py
"""
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "host"))
from ingest_config import DEFAULTS, as_bool, color, load_config
from ingest_copier import Abort, CardJob, COPYING, EMPTY, ERROR, PENDING
from ingest_discovery import Card
from ingest_emit import Emitter

# What app/proto.c's sscanf accepts for a slot line (4 pairs mandatory).
SLOT_RE = re.compile(
    r"^slot (\d+) (-?\d+) (-?\d+) (-?\d+) (idle|active|done|error|paused|pending)"
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
        self.cfg = load_config(None)
        self.cfg["dest"]["base"] = os.path.join(self.tmp.name, "dest")
        self.files = {
            "DCIM/100/IMG_0001.JPG": b"a" * 3000,
            "DCIM/100/IMG_0002.JPG": b"b" * 5000,
            "note.txt": b"hello",
        }
        make_card(self.src, self.files)

    def tearDown(self):
        self.tmp.cleanup()

    def job(self, src=None, **kw):
        card = Card("mock-0", "TESTCARD", "UUID-01", src or self.src, 20000)
        return CardJob(card, self.cfg, **kw)

    def test_full_pipeline_to_pending(self):
        j = self.job()
        j.run()
        self.assertEqual(j.state, PENDING)
        self.assertEqual(j.total_bytes, 8005)
        self.assertEqual(j.copied_bytes, 8005)
        self.assertEqual(j.verified_bytes, 8005)
        # every file copied byte-identical
        for rel, data in self.files.items():
            with open(os.path.join(j.dest, rel), "rb") as fh:
                self.assertEqual(fh.read(), data)
        # <ALGO>SUMS receipt: a 'hash  relpath' line per file, correct hashes
        algo = self.cfg["hash"]["algo"]
        sums = {}
        with open(j.manifest_path()) as fh:
            for line in fh:
                h, name = line.rstrip("\n").split(None, 1)
                sums[name] = h
        for rel, data in self.files.items():
            self.assertEqual(sums.get(rel), hashlib.new(algo, data).hexdigest())

    def test_dest_is_a_dated_dir(self):
        j = self.job()
        rel = os.path.relpath(j.dest, self.cfg["dest"]["base"]).split(os.sep)
        self.assertEqual(rel[0], "UUID-01")                    # base/<uuid>/...
        self.assertRegex(rel[1], r"^\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d")  # <date>/

    def test_same_card_reuses_its_dir(self):
        # dest is derived from the card's mount time, so re-running the same
        # ingest (e.g. after a daemon restart, mount still alive) lands in the
        # SAME dir -- rclone then skips what's copied instead of duplicating it.
        self.assertEqual(self.job().dest, self.job().dest)

    def test_copy_preserves_mtime(self):
        j = self.job()
        j.run()
        for rel in self.files:
            self.assertEqual(
                int(os.stat(os.path.join(self.src, rel)).st_mtime),
                int(os.stat(os.path.join(j.dest, rel)).st_mtime))

    def test_hash_mismatch_errors_and_keeps_everything(self):
        j = self.job()
        j.scan()
        j.copy()
        with open(os.path.join(j.dest, "note.txt"), "wb") as fh:
            fh.write(b"HELLO")             # corrupt the copy before verification
        self.assertRaises(Abort, j.verify)
        self.assertEqual(j.state, ERROR)
        self.assertEqual(j.error, "HASH FAIL")
        self.assertFalse(os.path.exists(j.manifest_path()))  # no manifest
        for rel in self.files:             # source untouched
            self.assertTrue(os.path.exists(os.path.join(self.src, rel)))

    def test_no_manifest_until_verified(self):
        j = self.job()
        j.scan()
        j.copy()
        self.assertFalse(os.path.exists(j.manifest_path()))

    def test_confirm_refused_unless_pending(self):
        j = self.job()
        j.scan()
        j.state = COPYING
        self.assertFalse(j.request_wipe())
        self.assertEqual(j.state, COPYING)

    def test_dry_run_wipe_deletes_nothing(self):
        j = self.job()                     # wipe_armed defaults to False
        j.run()
        self.assertTrue(j.request_wipe())
        self._await_state(j, EMPTY)
        for rel in self.files:
            self.assertTrue(os.path.exists(os.path.join(self.src, rel)),
                            "dry-run wipe must not delete %s" % rel)

    def test_armed_wipe_deletes_only_verified_files(self):
        j = self.job(wipe_armed=True)
        j.run()
        extra = os.path.join(self.src, "LATE.RAW")   # appears after the scan
        with open(extra, "wb") as fh:
            fh.write(b"late")
        self.assertTrue(j.request_wipe())
        self._await_state(j, EMPTY)
        for rel in self.files:
            self.assertFalse(os.path.exists(os.path.join(self.src, rel)))
        self.assertTrue(os.path.exists(extra), "unscanned file must survive")

    def test_no_data_card_is_clean_wipeable(self):
        # a card with no data -- only folders and/or 0-byte files -- is offered
        # as wipeable (to clean it), not stuck as a non-wipeable EMPTY card.
        src = os.path.join(self.tmp.name, "cleanme")
        os.makedirs(os.path.join(src, "DCIM", "100"))       # empty folders
        open(os.path.join(src, ".marker"), "wb").close()    # a 0-byte file
        j = self.job(src=src, wipe_armed=True)
        j.run()
        self.assertEqual(j.state, PENDING)                  # offered for cleaning
        self.assertTrue(j.request_wipe())
        self._await_state(j, EMPTY)
        self.assertFalse(os.path.exists(os.path.join(src, "DCIM")))
        self.assertFalse(os.path.exists(os.path.join(src, ".marker")))
        self.assertTrue(os.path.isdir(src))                 # mount root remains

    def test_truly_bare_card_is_empty_not_wipeable(self):
        src = os.path.join(self.tmp.name, "bare")
        os.makedirs(src)                                    # nothing at all
        j = self.job(src=src)
        j.run()
        self.assertEqual(j.state, EMPTY)

    def test_armed_wipe_removes_emptied_dirs(self):
        j = self.job(wipe_armed=True)
        j.run()
        self.assertTrue(j.request_wipe())
        self._await_state(j, EMPTY)
        # emptied subdirs are removed; the mount root itself is left alone
        self.assertFalse(os.path.exists(os.path.join(self.src, "DCIM")))
        self.assertTrue(os.path.isdir(self.src))

    def test_armed_wipe_refuses_source_changed_after_scan(self):
        j = self.job(wipe_armed=True)
        j.run()
        self.assertEqual(j.state, PENDING)
        victim = os.path.join(self.src, "DCIM/100/IMG_0001.JPG")  # sorts first
        with open(victim, "wb") as fh:
            fh.write(b"z" * 3000)          # same length, different bytes
        os.utime(victim, ns=(0, 0))        # ...and a different mtime
        self.assertTrue(j.request_wipe())
        self._await_state(j, ERROR)
        self.assertEqual(j.error, "SRC CHANGED")
        for rel in self.files:             # nothing deleted
            self.assertTrue(os.path.exists(os.path.join(self.src, rel)))

    def _await_state(self, job, state, timeout=5.0):
        deadline = time.monotonic() + timeout
        while job.state != state:
            self.assertLess(time.monotonic(), deadline,
                            "job stuck in %s" % job.state)
            time.sleep(0.01)


class ConfigTest(unittest.TestCase):
    def test_as_bool_rejects_quoted_false(self):
        for v in ("false", "0", "no", "off", "  False  ", ""):
            self.assertFalse(as_bool(v), "%r must be False" % v)
        for v in ("true", "1", "yes", "on", True):
            self.assertTrue(as_bool(v), "%r must be True" % v)
        self.assertFalse(as_bool(False))

    def test_color_accepts_int_and_str(self):
        self.assertEqual(color("#22C35E"), 0x22C35E)
        self.assertEqual(color("22c35e"), 0x22C35E)
        self.assertEqual(color(0x22C35E), 0x22C35E)


class EmitterTest(unittest.TestCase):
    def test_lines_fit_the_device_grammar(self):
        out = io.StringIO()
        em = Emitter(out, DEFAULTS["segments"])
        em.preamble()

        card = Card("mock-0", "A-VERY-LONG-CARD-LABEL-XYZ", "U", "/x",
                           10_000_000_000)
        job = CardJob.__new__(CardJob)   # no filesystem needed
        job.card, job.state, job.error = card, COPYING, ""
        job.dest = "/dest/U"
        job.total_bytes = 9_000_000_000
        job.copied_bytes = 5_000_000_000
        job.verified_bytes = 2_000_000_000
        job.uploaded_bytes = 1_000_000_000
        em.tick([job, None])
        lines = out.getvalue().splitlines()

        slots = [l for l in lines if l.startswith("slot ")]
        self.assertEqual(len(slots), 2)
        for l in slots:
            self.assertRegex(l, SLOT_RE)
        m = SLOT_RE.match(slots[0])
        self.assertEqual(int(m.group(2)), 10_000)      # size_mb
        nums = [int(x) for x in slots[0].split()[6:14:2]]
        # relative to the card's own 10 GB: uploaded/verified/copied/uncopied
        self.assertEqual(nums, [100, 100, 300, 400])
        self.assertLessEqual(sum(nums), 1000)          # relative scale
        self.assertLessEqual(len(m.group(7)), 23)      # MAX_LABEL - 1
        self.assertEqual(slots[1], "slot 1 -1 -1 -1 idle 0 0 0 0 0 0 0 0 empty")
        self.assertIn("hb", lines)
        self.assertTrue(any(l.startswith("path 0 ") for l in lines))
        self.assertIn("bg 202020", lines)
        # legend clear + 5 rows (uploaded/verified/copied/uncopied/free space)
        self.assertEqual(sum(l.startswith("legend ") for l in lines), 6)

    def test_removed_column_is_cleared_after_list_shrinks(self):
        # A card's column must be blanked when it's gone -- even though the
        # daemon then passes a shorter list (the removed column is off the end).
        out = io.StringIO()
        em = Emitter(out, DEFAULTS["segments"])
        job = CardJob.__new__(CardJob)
        job.card = Card("m", "C", "U", "/x", 1_000_000_000)
        job.state, job.error, job.dest = COPYING, "", "/d"
        job.total_bytes = job.copied_bytes = job.verified_bytes = 0
        job.uploaded_bytes = 0
        em.tick([job, job])                     # two cards -> columns 0 and 1
        out.truncate(0); out.seek(0)
        em.tick([job])                          # column 1's card removed
        lines = out.getvalue().splitlines()
        self.assertIn("slot 1 -1 -1 -1 idle 0 0 0 0 0 0 0 0 empty", lines)
        out.truncate(0); out.seek(0)
        em.tick([job])                          # already cleared -> not re-sent
        self.assertNotIn("slot 1", out.getvalue())


class UploaderTest(unittest.TestCase):
    def test_upload_verifies_against_remote_and_marks_done(self):
        import subprocess
        import uploader
        from ingest_copier import (manifest_name, read_uploaded, write_metadata)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = os.path.join(tmp.name, "dest")
        remote = os.path.join(tmp.name, "remote")   # local dir stands in for cloud
        d = os.path.join(base, "UUID-01", "2026-07-14_00-00-00")
        make_card(d, {"DCIM/IMG.JPG": b"x" * 2000, "note.txt": b"hi"})
        # receipt (written outside d, then moved in) + metadata, like the daemon
        sums = os.path.join(tmp.name, "sums")
        with open(sums, "w") as fo:
            subprocess.run(["rclone", "sha1sum", d], stdout=fo,
                           stderr=subprocess.DEVNULL, check=True)
        os.replace(sums, os.path.join(d, manifest_name("sha1")))
        write_metadata(d, {"total_bytes": 2002, "files": 2})   # copier's receipt
        os.makedirs(os.path.join(base, "UUID-02", "d"))  # no metadata -> ignored

        self.assertEqual(list(uploader.ready_dirs(base)), [d])
        self.assertTrue(uploader.upload_dir(d, base, remote, "sha1"))

        rd = os.path.join(remote, "UUID-01", "2026-07-14_00-00-00")
        self.assertTrue(os.path.exists(os.path.join(rd, "note.txt")))
        self.assertEqual(read_uploaded(d).get("uploaded_bytes"), 2002)
        # the proof: remote's own hashes match what we ingested
        loc = {l.split()[1]: l.split()[0]
               for l in open(os.path.join(d, "SHA1SUMS"))}
        rem = {l.split()[1]: l.split()[0]
               for l in open(os.path.join(d, "REMOTE_SHA1SUMS"))}
        for f in ("DCIM/IMG.JPG", "note.txt"):
            self.assertEqual(loc[f], rem[f])
        # uploaded -> not offered again
        self.assertEqual(list(uploader.ready_dirs(base)), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
