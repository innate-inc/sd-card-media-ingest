"""Configuration: defaults, TOML loading, and value coercion.

Every value has a built-in default; a TOML file (or a CLI flag) overrides it.
"""

DEFAULTS = {
    "serial": {"vid": "2e8a", "pid": ""},   # "" vid+pid = stdout/stdin pipe mode
    "hub": {"path_prefix": ""},             # /dev/disk/by-path prefix of the hub
    "dest": {"base": "/media/jetson1/jetson_backup/ingest/"},  # base/<uuid>/<date>/
    "hash": {"algo": "md5"},                # cheapest hashlib algo; a copy check
    "segments": {
        # Okabe-Ito-ish, colourblind-safe; meanings are the host's to assign.
        "uploaded": "#22C35E",   # copied AND verified (manifest-backed)
        "copied": "#0072B2",     # copied, not yet verified
        "uncopied": "#E69F00",   # still only on the card
        "empty": "#202020",      # free space on the card (the `bg` colour)
        "numbers": True,
    },
    "poll": {"interval_ms": 500},
    "wipe": {"enabled": False},              # real deletion also needs --enable-wipe
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
