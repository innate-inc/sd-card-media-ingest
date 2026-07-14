"""Configuration: defaults, TOML loading, value coercion, and logging setup.

Every value has a built-in default; a TOML file (or a CLI flag) overrides it.
"""
import logging
import os

BASE_CONFIG = "ingest.toml"       # tracked defaults (committed to git)
LOCAL_CONFIG = "config.toml"      # local overrides (gitignored); layered on top


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
    # Readers are found as the drives plugged into this hub (by USB vid:pid);
    # picks up SD readers + an SSD on the hub, not the nvme or the display board.
    "hub": {"vid": "1a40", "pid": "0101", "path_prefix": ""},  # Terminus hub
    "dest": {"base": "/media/jetson1/jetson_backup/ingest/"},  # base/<label>-<uuid>/<date>/
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


def load_config(*paths):
    """DEFAULTS overlaid with each TOML file in order -- later files win, one
    level deep like the tables. Missing (or None) paths are skipped, so the
    usual call is load_config(BASE_CONFIG, LOCAL_CONFIG): tracked defaults, then
    gitignored local overrides on top."""
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        import tomllib  # stdlib in Python >= 3.11 (flake pins 3.12)
        with open(path, "rb") as fh:
            user = tomllib.load(fh)
        for section, values in user.items():
            cfg.setdefault(section, {}).update(values)
    return cfg


def config_paths(override):
    """The config files to load: an explicit --config (alone), else the tracked
    base + local overrides. Returns a list for load_config(*...)."""
    return [override] if override else [BASE_CONFIG, LOCAL_CONFIG]


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
