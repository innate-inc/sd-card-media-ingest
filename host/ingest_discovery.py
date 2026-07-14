"""Discovery: which slots exist, and what card (if any) is in each.

A "slot" is a fixed physical position (a reader LUN); its index never moves
while the daemon runs, so bars hold their place on the display. A slot is
occupied when its block device reports non-zero size (media present).
"""
import hashlib
import logging
import os
import time

log = logging.getLogger("ingest")

# slots() yields, per slot, one of: a Card (media present), None (definitely
# absent), or UNKNOWN (a probe hit a transient error -- leave the slot's job
# alone rather than falsely declaring the card removed mid-copy).
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
    physical order; the /dev/sdX letters are never trusted.
    """

    BY_PATH = "/dev/disk/by-path"

    def __init__(self, hub_cfg):
        # Prefer matching readers by their USB vid:pid (robust to which port the
        # hub is in, and can't pick up the target SSD or the display board); fall
        # back to a /dev/disk/by-path prefix if one is configured.
        self._cache = {}                  # ident -> resolved Card (held while present)
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
            return None
        cached = self._cache.get(ident)
        if cached is not None:
            return cached                     # identity is fixed while inserted
        part, uuid = self._partition(dev)
        mnt = self._mountpoint(part or dev)
        label = self._fslabel(part or dev) or (uuid or node)[:12]
        card = Card(ident, label, uuid or node, mnt, sectors * 512)
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
