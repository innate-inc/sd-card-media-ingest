#!/usr/bin/env python3
"""Unit tests for the ingest daemon: copier, uploader, view and web UI.

Runs the real CardJob pipeline over a fake card tree in a temp dir and asserts
the locked rules: fresh dated dest dir, metadata preserved, verify-before-
manifest, hash-mismatch keeps the card, wipe only on confirm of a pending slot,
dry-run wipe deletes nothing, the view's segment maths stays exact, and the
web confirm refuses a stale page. Stdlib only:

    python3 tests/test_ingest.py
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "host"))
from ingest_config import DEFAULTS, as_bool, color, load_config
from ingest_copier import Abort, CardJob, COPYING, EMPTY, ERROR, PENDING
from ingest_discovery import Card


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


class ViewTest(unittest.TestCase):
    def test_segments_are_permille_of_the_cards_own_capacity(self):
        """The maths the device protocol used to carry, now feeding HTML:
        cumulative boundaries rounded then differenced, so the stack always
        sums to exactly full and the free remainder never jitters."""
        from ingest_view import View
        v = View(DEFAULTS["segments"])
        card = Card("mock-0", "A-VERY-LONG-CARD-LABEL-XYZ", "U", "/x",
                    10_000_000_000)
        job = CardJob.__new__(CardJob)      # no filesystem needed
        job.card, job.state, job.error = card, COPYING, ""
        job.dest = "/dest/U"
        job.total_bytes = 9_000_000_000
        job.copied_bytes = 5_000_000_000
        job.verified_bytes = 2_000_000_000
        job.uploaded_bytes = 1_000_000_000

        cards = v.cards([job, None])
        self.assertEqual(len(cards), 1)          # the empty column is absent
        c = cards[0]
        self.assertEqual(c["size_mb"], 10_000)
        self.assertEqual(c["uuid"], "U")
        self.assertFalse(c["pending"])           # only PENDING may be confirmed
        # relative to the card's own 10 GB: uploaded/verified/copied/uncopied
        self.assertEqual([pm for _n, pm, _c in c["segments"]],
                         [100, 100, 300, 400])
        self.assertEqual(sum(pm for _n, pm, _c in c["segments"]) + c["free"],
                         1000)

    def test_pending_card_is_marked_confirmable(self):
        from ingest_view import View
        v = View(DEFAULTS["segments"])
        job = CardJob.__new__(CardJob)
        job.card = Card("m", "C", "UUID-9", "/x", 1_000_000_000)
        job.state, job.error, job.dest = PENDING, "", "/d"
        job.total_bytes = job.copied_bytes = job.verified_bytes = 0
        job.uploaded_bytes = 0
        c = v.cards([job])[0]
        self.assertTrue(c["pending"])
        self.assertEqual(c["status"], "pending")


class WebTest(unittest.TestCase):
    """The page is now the confirm channel, so its interlock is load-bearing:
    a confirm must name a card that is still in that slot and still pending."""

    CARD = {"col": 0, "uuid": "UUID-1", "label": "CARD", "status": "pending",
            "pending": True, "size_mb": 32000, "eta_s": -1, "kbps": -1,
            "segments": [("uploaded", 400, "#009e73")], "free": 600,
            "path": "/dest/CARD", "wipe_armed": True}

    def _serve(self, **over):
        import queue
        from ingest_web import Station, serve
        card = dict(self.CARD, **over)
        st = Station()
        st.configure([("#009e73", "uploaded")], "#202020", True)
        st.publish([card])
        q = queue.Queue()
        srv = serve("127.0.0.1:0", st, q)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d/" % srv.server_address[1], q

    @staticmethod
    def _post(url, **fields):
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode(fields).encode()
        return urllib.request.urlopen(urllib.request.Request(url, data=data))

    def test_get_renders_the_card_and_a_confirm_form(self):
        import urllib.request
        url, _q = self._serve()
        page = urllib.request.urlopen(url).read().decode()
        self.assertIn("ingest station", page)
        self.assertIn('class="bar"', page)
        self.assertIn('name="uuid" value="UUID-1"', page)
        self.assertIn("wipe ARMED", page)
        self.assertNotIn("<script", page)          # no JS, on purpose

    def test_no_confirm_form_unless_the_card_is_pending(self):
        import urllib.request
        url, _q = self._serve(pending=False, status="active")
        page = urllib.request.urlopen(url).read().decode()
        self.assertNotIn("<form", page)

    def test_matching_confirm_enqueues_that_column(self):
        url, q = self._serve()
        r = self._post(url, col="0", uuid="UUID-1")   # 303 -> followed to GET /
        self.assertEqual(r.status, 200)
        self.assertEqual(q.get_nowait(), 0)

    def test_stale_uuid_is_refused_and_confirms_nothing(self):
        """The failure a physical button cannot have: the page showed one card,
        the operator swapped in another, then clicked."""
        import queue
        import urllib.error
        url, q = self._serve()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post(url, col="0", uuid="A-DIFFERENT-CARD")
        self.assertEqual(cm.exception.code, 409)
        self.assertRaises(queue.Empty, q.get_nowait)

    def test_confirm_for_a_non_pending_card_is_refused(self):
        import queue
        import urllib.error
        url, q = self._serve(pending=False, status="active")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post(url, col="0", uuid="UUID-1")
        self.assertEqual(cm.exception.code, 409)
        self.assertRaises(queue.Empty, q.get_nowait)

    def test_garbage_form_fields_are_refused_not_crashed(self):
        import queue
        import urllib.error
        url, q = self._serve()
        for fields in ({"col": "nope", "uuid": "UUID-1"}, {"col": "0"},
                       {}, {"col": "-1", "uuid": ""}):
            with self.subTest(fields=fields):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    self._post(url, **fields)
                self.assertEqual(cm.exception.code, 409)
        self.assertRaises(queue.Empty, q.get_nowait)


class UploaderTest(unittest.TestCase):
    def test_upload_progress_live_then_done(self):
        from ingest_copier import (clear_uploading, upload_progress,
                                    write_uploaded, write_uploading)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = os.path.join(tmp.name, "card", "2026-01-01_00-00-00")
        os.makedirs(d)
        self.assertEqual(upload_progress(d), 0)
        write_uploading(d, 500)
        self.assertEqual(upload_progress(d), 500)          # live count
        # the live file is a sibling, NOT inside the dir rclone copies
        self.assertEqual(os.listdir(d), [])
        write_uploaded(d, {"uploaded_bytes": 2000})
        self.assertEqual(upload_progress(d), 2000)         # done wins over live
        clear_uploading(d)
        self.assertEqual(upload_progress(d), 2000)

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

    def test_uploads_in_progress_dir_before_finalizing(self):
        import uploader
        from ingest_copier import (clear_copying, mark_copying, read_uploaded,
                                    write_metadata)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = os.path.join(tmp.name, "dest")
        remote = os.path.join(tmp.name, "remote")
        d = os.path.join(base, "UUID-9", "2026-01-01_00-00-00")
        make_card(d, {"a.mp4": b"x" * 1000, "b.mp4.partial": b"half"})
        mark_copying(d)                            # copier still writing this dir

        # still copying: offered, completed files go up (partials excluded), but
        # NOT finalised (no metadata -> no uploaded.json).
        self.assertIn(d, list(uploader.ready_dirs(base)))
        self.assertFalse(uploader.upload_dir(d, base, remote, "sha1"))
        rd = os.path.join(remote, "UUID-9", "2026-01-01_00-00-00")
        self.assertTrue(os.path.exists(os.path.join(rd, "a.mp4")))
        self.assertFalse(os.path.exists(os.path.join(rd, "b.mp4.partial")))
        self.assertFalse(read_uploaded(d))

        # copier finishes: partial renamed, marker cleared, metadata written.
        os.remove(os.path.join(d, "b.mp4.partial"))
        make_card(d, {"b.mp4": b"y" * 500})
        clear_copying(d)
        write_metadata(d, {"total_bytes": 1500, "files": 2})
        self.assertTrue(uploader.upload_dir(d, base, remote, "sha1"))
        self.assertEqual(read_uploaded(d).get("uploaded_bytes"), 1500)
        self.assertTrue(os.path.exists(os.path.join(rd, "b.mp4")))


class BackupPathTest(unittest.TestCase):
    def test_backup_targets_drops_incomplete_entries(self):
        import uploader
        cfg = {"backup": {"paths": [{"src": "/a/", "dst": "/x/"},
                                    {"src": "/b"},          # no dst -> dropped
                                    {"dst": "y"}]}}         # no src -> dropped
        self.assertEqual(uploader.backup_targets(cfg), [("/a", "x")])

    def test_empty_dst_means_the_remote_base_itself(self):
        """One entry can cover a whole tree by mirroring onto the base: the
        subdirs the card uploader already wrote there become skips."""
        import uploader
        cfg = {"backup": {"paths": [{"src": "/media/x/ingest", "dst": ""}]}}
        self.assertEqual(uploader.backup_targets(cfg),
                         [("/media/x/ingest", "")])

    def test_root_dst_targets_the_base_with_no_double_slash(self):
        import uploader
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "tree"); remote = os.path.join(tmp.name, "r")
        make_card(src, {"card-A/2026-01-01/a.txt": b"x"})
        self.assertEqual(uploader.backup_dir(src, "", remote), "verified")
        # lands at <remote>/card-A/..., i.e. the same level the card uploader
        # writes to -- not nested under an extra directory
        self.assertTrue(os.path.exists(
            os.path.join(remote, "card-A", "2026-01-01", "a.txt")))

    def test_backup_targets_absent_section_is_empty(self):
        import uploader
        self.assertEqual(uploader.backup_targets({}), [])

    def test_backup_mirrors_plain_dir_writing_nothing_into_it(self):
        import uploader
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "immich", "library")
        remote = os.path.join(tmp.name, "remote")
        make_card(src, {"2026/IMG.jpg": b"x" * 100, "note.txt": b"hi"})
        before = sorted(os.listdir(src))

        self.assertTrue(uploader.backup_dir(src, "immich/library", remote))
        rd = os.path.join(remote, "immich", "library")
        self.assertTrue(os.path.exists(os.path.join(rd, "note.txt")))
        self.assertTrue(os.path.exists(os.path.join(rd, "2026", "IMG.jpg")))
        # no uploaded.json / REMOTE_SHA1SUMS: Immich owns this directory
        self.assertEqual(sorted(os.listdir(src)), before)

    def test_backup_skips_missing_source_without_touching_remote(self):
        import uploader
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        remote = os.path.join(tmp.name, "remote")
        os.makedirs(remote)
        gone = os.path.join(tmp.name, "not-mounted", "library")
        # the unmounted-volume case: no copy, no delete, just a loud skip
        self.assertFalse(uploader.backup_dir(gone, "immich/library", remote))
        self.assertEqual(os.listdir(remote), [])

    def test_backup_sweep_counts_only_verified(self):
        import uploader
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        good = os.path.join(tmp.name, "good")
        make_card(good, {"a.txt": b"a"})
        cfg = {"backup": {"paths": [
            {"src": good, "dst": "good"},
            {"src": os.path.join(tmp.name, "missing"), "dst": "missing"}]}}
        self.assertEqual(uploader.backup_sweep(cfg, os.path.join(tmp.name, "r")),
                         (1, 2))

    def test_card_sweep_ignores_backup_style_dirs(self):
        """A plain folder inside dest_base (e.g. karmanyaah-google-photos after
        the ingest move) has no metadata.json and no .copying, so the card
        pipeline must not pick it up."""
        import uploader
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = os.path.join(tmp.name, "dest")
        make_card(os.path.join(base, "karmanyaah-google-photos", "2019"),
                  {"pic.jpg": b"x"})
        self.assertEqual(list(uploader.ready_dirs(base)), [])


class BackupRobustnessTest(unittest.TestCase):
    """A malformed [backup] line must never crash the daemon: systemd would
    crash-loop it and card uploads -- and thus the wipe interlock -- stall."""

    def test_malformed_entries_never_raise(self):
        import uploader
        for paths in ("oops", ["just-a-string"], [["a", "b"]], [None], [42],
                      [{"src": 123, "dst": "x"}], [{"src": "/a", "dst": None}],
                      {"src": "/a"}):
            with self.subTest(paths=paths):
                self.assertEqual(uploader.backup_targets(
                    {"backup": {"paths": paths}}), [])

    def test_good_entries_survive_beside_bad_ones(self):
        import uploader
        cfg = {"backup": {"paths": ["junk", {"src": "/a", "dst": "x"}, 7]}}
        self.assertEqual(uploader.backup_targets(cfg), [("/a", "x")])

    def test_dst_cannot_escape_the_remote_base(self):
        import uploader
        cfg = {"backup": {"paths": [{"src": "/a", "dst": "../../elsewhere"}]}}
        self.assertEqual(uploader.backup_targets(cfg), [])

    def test_interval_is_never_fatal(self):
        import uploader
        for raw in ("6h", None, [], "", -5, 0):
            with self.subTest(raw=raw):
                self.assertEqual(
                    uploader.backup_every_seconds({"backup": {"interval": raw}}), 0.0)
        self.assertEqual(
            uploader.backup_every_seconds({"backup": {"interval": "900"}}), 900.0)
        self.assertEqual(uploader.backup_every_seconds({}), 0.0)

    def test_empty_source_is_not_reported_verified(self):
        import uploader
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "mountpoint")     # exists, nothing in it
        os.makedirs(src)
        remote = os.path.join(tmp.name, "remote")
        self.assertFalse(uploader.backup_dir(src, "immich/library", remote))
        self.assertFalse(os.path.exists(remote))

    def test_partial_named_files_are_backed_up_not_skipped(self):
        """The card path excludes *.partial because the copier owns those
        temps. On a foreign directory that would skip a real file and then fail
        the unfiltered check on every sweep, forever."""
        import uploader
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "src")
        remote = os.path.join(tmp.name, "remote")
        make_card(src, {"video.partial": b"real file, real name", "a.jpg": b"x"})
        self.assertTrue(uploader.backup_dir(src, "d", remote))
        self.assertTrue(os.path.exists(os.path.join(remote, "d", "video.partial")))

    def test_card_uploads_still_exclude_partials(self):
        """...while the card path keeps the exclude it needs."""
        import uploader
        from ingest_copier import mark_copying
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = os.path.join(tmp.name, "dest")
        remote = os.path.join(tmp.name, "remote")
        d = os.path.join(base, "UUID-1", "2026-01-01_00-00-00")
        make_card(d, {"a.mp4": b"x" * 10, "b.mp4.partial": b"half"})
        mark_copying(d)
        uploader.upload_dir(d, base, remote, "sha1")
        rd = os.path.join(remote, "UUID-1", "2026-01-01_00-00-00")
        self.assertTrue(os.path.exists(os.path.join(rd, "a.mp4")))
        self.assertFalse(os.path.exists(os.path.join(rd, "b.mp4.partial")))


class BackupCadenceTest(unittest.TestCase):
    """The copy pass and the hash scrub run on separate clocks: copying is a
    listing diff, checking re-reads every byte of src."""

    def _spy(self, uploader):
        """Record the rclone subcommands actually invoked."""
        calls = []
        real = uploader._rclone
        def spy(args, **kw):
            calls.append(args[0])
            return real(args, **kw)
        uploader._rclone = spy
        self.addCleanup(setattr, uploader, "_rclone", real)
        return calls

    def test_unchanged_path_is_not_rehashed_but_new_data_is(self):
        import uploader
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "src")
        remote = os.path.join(tmp.name, "remote")
        make_card(src, {"a.txt": b"x"})
        self.assertEqual(uploader.backup_dir(src, "d", remote), "verified")

        calls = self._spy(uploader)
        # nothing moved and no scrub due -> copy runs, check is skipped
        self.assertEqual(uploader.backup_dir(src, "d", remote, scrub=False),
                         "unchanged")
        self.assertNotIn("check", calls)
        # a new file moves bytes -> checked immediately, scrub=False or not
        make_card(src, {"b.txt": b"y"})
        self.assertEqual(uploader.backup_dir(src, "d", remote, scrub=False),
                         "verified")
        self.assertIn("check", calls)

    def test_scrub_rehashes_even_when_nothing_moved(self):
        import uploader
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "src")
        remote = os.path.join(tmp.name, "remote")
        make_card(src, {"a.txt": b"x"})
        uploader.backup_dir(src, "d", remote)
        calls = self._spy(uploader)
        self.assertEqual(uploader.backup_dir(src, "d", remote, scrub=True),
                         "verified")
        self.assertIn("check", calls)

    def test_sweep_defers_scrub_until_verify_every_elapses(self):
        import uploader
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "src")
        remote = os.path.join(tmp.name, "remote")
        make_card(src, {"a.txt": b"x"})
        cfg = {"backup": {"paths": [{"src": src, "dst": "d"}]}}
        last = {}
        self.assertEqual(uploader.backup_sweep(cfg, remote, 3600, last), (1, 1))
        first = last["d"]

        calls = self._spy(uploader)
        self.assertEqual(uploader.backup_sweep(cfg, remote, 3600, last), (1, 1))
        self.assertNotIn("check", calls)          # scrub not due yet
        self.assertEqual(last["d"], first)        # timestamp not advanced

        last["d"] -= 7200                          # pretend a day went by
        self.assertEqual(uploader.backup_sweep(cfg, remote, 3600, last), (1, 1))
        self.assertIn("check", calls)             # scrub now due
        self.assertGreater(last["d"], first)

    def test_sweep_without_a_last_map_checks_every_pass(self):
        import uploader
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "src")
        remote = os.path.join(tmp.name, "remote")
        make_card(src, {"a.txt": b"x"})
        cfg = {"backup": {"paths": [{"src": src, "dst": "d"}]}}
        uploader.backup_sweep(cfg, remote)
        calls = self._spy(uploader)
        uploader.backup_sweep(cfg, remote)         # default: conservative
        self.assertIn("check", calls)

    def test_verify_every_is_never_fatal_and_defaults_conservatively(self):
        import uploader
        for raw in ("daily", None, [], -1, 0):
            with self.subTest(raw=raw):
                self.assertEqual(uploader.backup_verify_seconds(
                    {"backup": {"verify_every": raw}}), 0.0)
        self.assertEqual(uploader.backup_verify_seconds(
            {"backup": {"verify_every": "86400"}}), 86400.0)
        self.assertEqual(uploader.backup_verify_seconds({}), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
