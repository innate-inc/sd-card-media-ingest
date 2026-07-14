"""Configuration: defaults, TOML loading, value coercion, and logging setup.

Every value has a built-in default; a TOML file (or a CLI flag) overrides it.
"""
import logging


def setup_logging():
    """INFO+ to stderr with a timestamp (journald adds its own too). Modules log
    via logging.getLogger("ingest")."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")


def human_bytes(n):
    """Human-readable size, e.g. 238.0 GB."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f TB" % n

DEFAULTS = {
    "serial": {"vid": "2e8a", "pid": ""},   # "" vid+pid = stdout/stdin pipe mode
    "hub": {"path_prefix": ""},             # /dev/disk/by-path prefix of the hub
    "dest": {"base": "/media/jetson1/jetson_backup/ingest/"},  # base/<uuid>/<date>/
    "hash": {"algo": "sha1"},               # the common hash across Drive + B2
    "segments": {
        # Okabe-Ito, colourblind-safe; the stages the bar climbs through.
        "uncopied": "#E69F00",   # orange - still only on the card
        "copied":   "#F0E442",   # yellow - copied to local disk, not verified
        "verified": "#0072B2",   # blue   - hash-verified local copy
        "uploaded": "#009E73",   # green  - pushed to the cloud remote
        "empty":    "#202020",   # bg / free space
        "numbers":  True,
    },
    "poll": {"interval_ms": 500},
    "wipe": {"enabled": False},              # real deletion also needs the env var
    "remote": {"base": ""},                  # rclone dest for the uploader; "" = off
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
    """Strict truthiness: a TOML bool stays itself, but a quoted string like
    "false"/"0"/"no" must NOT read as True (plain Python truthiness treats any
    non-empty string as True)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)
