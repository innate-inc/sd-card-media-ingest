"""Discovery: which slots exist, and what card (if any) is in each.

A "slot" here is a fixed physical position (a reader LUN); its index never
moves while the daemon runs. A slot is occupied when its block device reports
non-zero size (media present). The daemon assigns each present card a display
column of its own (insertion order); the physical slot index is internal.
"""
import hashlib
import logging
import os
import re
import subprocess
import time

log = logging.getLogger("ingest")

# slots() yields, per slot, one of: a Card (media present), None (definitely
# absent), or UNKNOWN (a probe hit a transient error -- leave the slot's job
# alone rather than falsely declaring the card removed mid-copy).
UNKNOWN = object()


class Card:
    """A present card: identity + where to read it from."""

    def __init__(self, ident, label, uuid, mountpoint, capacity_bytes,
                 fs_label=""):
        self.ident = ident                # stable slot identity (by-path / mock)
        self.label = label                # short name for the display column
        self.uuid = uuid                  # partition UUID -> dest sub-dir
        self.fs_label = fs_label          # real filesystem label ("" if none)
        self.mountpoint = mountpoint      # where its files are readable
        self.capacity_bytes = capacity_bytes


class HubDiscovery:
    """Real readers: /dev/disk/by-path entries under the configured hub prefix.

    The by-path string encodes USB topology, so sorting it gives a stable
    physical order; the /dev/sdX letters are never trusted.
    """

    BY_PATH = "/dev/disk/by-path"
    MOUNT_ROOT = "/run/ingest"            # where we auto-mount cards (headless)

    def __init__(self, hub_cfg, mount=False):
        # Prefer matching readers by their USB vid:pid (robust to which port the
        # hub is in, and can't pick up the target SSD or the display board); fall
        # back to a /dev/disk/by-path prefix if one is configured.
        self._cache = {}                  # ident -> resolved Card (held while present)
        self.mount = mount                # auto-mount unmounted cards ourselves?
        self._mounts = {}                 # ident -> mountpoint we created
        self._mount_failed = set()        # idents we've already warned we can't mount
        self._released = {}               # ident -> Card left unmounted after a wipe
        vid = (hub_cfg.get("vid") or "").lower()
        pid = (hub_cfg.get("pid") or "").lower()
        prefix = hub_cfg.get("path_prefix") or ""
        if vid:
            self.slot_ids = _slots_behind_hub(vid, pid)
            how = "behind hub %s:%s" % (vid, pid or "*")
        else:
            self.slot_ids = _slots_by_prefix(prefix)
            how = "by-path prefix %r" % prefix
        self.slot_ids = sorted(self.slot_ids)   # by-path order = physical order
        (log.info if self.slot_ids else log.warning)(
            "discovery: %d reader slot(s) via %s", len(self.slot_ids), how)

    def slots(self):
        """-> list of Card / None / UNKNOWN, one per fixed slot, in order."""
        out = []
        for ident in self.slot_ids:
            dev = os.path.realpath(os.path.join(self.BY_PATH, ident))
            out.append(self._probe(ident, dev))
        return out

    def _probe(self, ident, dev):
        # The only thing that changes tick-to-tick is presence, so read just the
        # cheap sysfs size each poll and resolve the (stable) card identity once.
        node = os.path.basename(dev)
        try:
            with open("/sys/class/block/%s/size" % node) as fh:
                sectors = int(fh.read())
        except (OSError, ValueError):
            return UNKNOWN                    # transient read error, not removal
        if sectors == 0:
            self._cache.pop(ident, None)      # media gone; forget it
            self._unmount(ident)              # drop any mount we made for it
            self._released.pop(ident, None)
            self._mount_failed.discard(ident)
            return None
        released = self._released.get(ident)
        if released is not None:
            return released                   # wiped: present but left unmounted
        cached = self._cache.get(ident)
        if cached is not None:
            return cached                     # identity is fixed while inserted
        part, uuid = self._partition(dev)
        uuid = _blkid(part or dev, "UUID") or uuid    # direct read beats the udev race
        mnt = self._mountpoint(part or dev)
        if mnt is None and self.mount:        # headless: mount it ourselves
            mnt = self._automount(part or dev, ident)
        flabel = _blkid(part or dev, "LABEL") or self._fslabel(part or dev)
        label = flabel or (uuid or node)[:12]
        card = Card(ident, label, uuid or node, mnt, sectors * 512,
                    fs_label=flabel or "")
        if mnt is not None:                   # only cache a card we can read;
            self._cache[ident] = card         # keep re-resolving until it mounts
        return card

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
        name = _reverse_symlink("/dev/disk/by-label", dev)
        # by-label names are udev-escaped (space -> \x20); decode for the display.
        return re.sub(r"\\x([0-9a-fA-F]{2})",
                      lambda m: chr(int(m.group(1), 16)), name) if name else name

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

    def _automount(self, part, ident):
        """Mount a present-but-unmounted card so the copier can read it (a
        headless station has no desktop auto-mounter). Mounts read-write --
        the wipe deletes from here -- to a per-device dir under MOUNT_ROOT, and
        remembers it so removal unmounts it. Returns the mountpoint, or None."""
        mp = os.path.join(self.MOUNT_ROOT, os.path.basename(part))
        try:
            os.makedirs(mp, exist_ok=True)
            if os.path.ismount(mp):           # a stale mount (e.g. after a crash)
                subprocess.run(["umount", "-l", mp], capture_output=True)
            subprocess.run(
                ["mount", "-o", "noatime,nosuid,nodev,noexec", part, mp],
                check=True, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            if ident not in self._mount_failed:   # once, not every tick
                self._mount_failed.add(ident)
                err = getattr(e, "stderr", b"") or b""
                log.warning("could not auto-mount %s: %s", part,
                            err.decode("utf-8", "replace").strip() or e)
            return None
        self._mounts[ident] = mp
        self._mount_failed.discard(ident)
        log.info("auto-mounted %s at %s (rw)", part, mp)
        return mp

    def release(self, ident):
        """After a wipe: unmount the card and stop auto-mounting it, so its
        filesystem is flushed and it's safe to pull. It still reports 'present'
        (unmounted) until physically removed, then everything is forgotten."""
        card = self._cache.pop(ident, None)
        self._unmount(ident)
        if card is not None:
            card.mountpoint = None
            self._released[ident] = card

    def _unmount(self, ident):
        """Unmount (and clean up) a card we auto-mounted; no-op otherwise."""
        mp = self._mounts.pop(ident, None)
        if not mp:
            return
        try:
            subprocess.run(["umount", mp], check=True, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            subprocess.run(["umount", "-l", mp], capture_output=True)  # lazy fallback
        try:
            os.rmdir(mp)
        except OSError:
            pass
        log.info("unmounted %s", mp)


def _blkid(part, tag):
    """Read a filesystem tag (UUID / LABEL) straight from the device with
    `blkid -p` -- unlike the /dev/disk/by-* symlinks, it doesn't wait for udev,
    which our instant auto-mount can outrun (leaving uuid falling back to the
    sdX node). Needs root to read the raw device; returns None if unavailable."""
    try:
        out = subprocess.run(["blkid", "-p", "-s", tag, "-o", "value", part],
                             capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8", "replace").strip() or None


def _slots_by_prefix(prefix):
    """by-path idents (whole disks) starting with `prefix`."""
    try:
        names = os.listdir(HubDiscovery.BY_PATH)
    except OSError:
        return []
    return [n for n in names if n.startswith(prefix) and "-part" not in n]


def _slots_behind_hub(vid, pid):
    """by-path idents of every whole-disk block device plugged into a hub whose
    USB vid[:pid] matches -- card-reader LUNs, an SSD, anything on the hub."""
    out = []
    try:
        blocks = os.listdir("/sys/block")
    except OSError:
        return []
    for dev in blocks:
        if _behind_hub("/sys/block/" + dev, vid, pid):
            bp = _bypath_of(dev)
            if bp:
                out.append(bp)
    return out


def _behind_hub(sysblock, vid, pid):
    """True if any USB ancestor of the block device matches vid[:pid] -- i.e. the
    disk is plugged into that hub (so it excludes the internal nvme and anything
    on a different controller)."""
    try:
        p = os.path.realpath(os.path.join(sysblock, "device"))
    except OSError:
        return False
    for _ in range(16):                   # walk up the sysfs USB topology
        vf = os.path.join(p, "idVendor")
        if os.path.exists(vf):
            try:
                v = open(vf).read().strip().lower()
                pd = open(os.path.join(p, "idProduct")).read().strip().lower()
                if v == vid and (not pid or pd == pid):
                    return True
            except OSError:
                pass
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return False


def _bypath_of(dev):
    """The stable /dev/disk/by-path name for a whole disk (the plain `-usb-`
    one, preferred over usbv2/usbv3 duplicates)."""
    try:
        real = os.path.realpath("/dev/" + dev)
        names = sorted(os.listdir(HubDiscovery.BY_PATH))
    except OSError:
        return None
    cands = [n for n in names if "-part" not in n and "-usb" in n
             and os.path.realpath(os.path.join(HubDiscovery.BY_PATH, n)) == real]
    for n in cands:
        if "-usb-" in n:                  # plain USB link, not usbv2/usbv3
            return n
    return cands[0] if cands else None


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
    remove_after_s = None keeps the card in. Capacity is padded ~25% over the
    used bytes so the bars visibly fill most of the column.
    """

    SPECS = [
        ("SANDISK64", 6, 2048, 0.0, None),
        ("EXTREME32", 4, 1536, 0.0, None),
        ("LEXAR64",   5, 1024, 2.0, None),
        (None,        0, 0,    0.0, None),    # a slot that stays empty
        ("PNY256",    3, 768,  4.0, None),
    ]

    def __init__(self, root):
        self.t0 = time.monotonic()
        self.cards = []
        for i, (name, nfiles, kib, ins, rm) in enumerate(self.SPECS):
            card = None
            if name:
                src = os.path.join(root, "card%d" % i)
                used = _make_fake_card(src, nfiles, kib, seed=i)
                card = Card("mock-%d" % i, name, "MOCK-%04d" % (0x1000 + i),
                            src, int(used * 1.25), fs_label=name)
            self.cards.append((card, ins, rm))

    def slots(self):
        t = time.monotonic() - self.t0
        return [card if card and ins <= t and (rm is None or t < rm) else None
                for card, ins, rm in self.cards]

    def release(self, ident):
        pass                                  # no real mounts in --dry-run


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
