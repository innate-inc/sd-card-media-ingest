#!/usr/bin/env python3
"""ingest-display -- render live ingest progress bars on the RP2350 LCD.

The output layer for the SD-card / USB ingest station. It owns the little
172x320 LCD and draws one labelled progress bar per slot. The set of slots is
*dynamic*: whoever feeds it decides how many bars there are, their order, and
their labels -- so the station's discovery layer can list the currently-plugged
devices in physical (spatial) port order and hand them over. This program just
renders what it is told.

Updates arrive as JSON objects, one per line, on stdin:

    # declare / replace the ordered slot set (this is how count + order + labels
    # are set -- e.g. from physical USB topology):
    {"slots": [{"id": "1-1.1", "label": "A"}, {"id": "1-1.2", "label": "B"}]}

    # update progress/status by slot id or label:
    {"1-1.1": 0.42}                 # 0..1 (>1 treated as a percentage)
    {"1-1.2": "done"}               # status; "done" also sets 100%
    {"1-1.1": {"progress": 0.5, "status": "active", "label": "A"}}
    {"1-1.2": null}                 # reset that slot

    # a full tick can also carry everything at once:
    {"slots": [{"id": "1-1.1", "label": "A", "progress": 0.3, "status": "active"}]}

Design (unixy):
  * Single owner: an flock on a lock file means only one instance runs at a
    time, so nothing else can fight over the display. All updates funnel through
    that one process's stdin.
  * Everything is configurable from a TOML file (screen, layout, colours,
    fonts). See ingest_display.toml.
  * stdin/JSON in, pixels out. Reads until EOF, then exits cleanly.
"""
import argparse
import fcntl
import json
import os
import select
import sys
import time

from PIL import Image, ImageDraw, ImageFont

import wire


def _load_toml(fh):
    """Import a TOML parser lazily (only needed when a config file exists)."""
    try:
        import tomllib  # Python >= 3.11
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib
    return tomllib.load(fh)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "serial": {"port": "auto", "baud": 115200, "ack_timeout": 1.0},
    # Physical panel is 172x320. rotate=90 mounts it sideways (landscape,
    # 320x172 drawing area) -- the default. Use 270 to turn it the other way.
    "screen": {"width": 172, "height": 320, "background": "#000000", "rotate": 90},
    "title": {"text": "", "color": "#ffffff", "height": 0, "font": "", "font_size": 16},
    "empty": {"text": "waiting for devices…", "color": "#9aa0a6",
              "font": "", "font_size": 14},
    "layout": {
        "style": "columns",  # "columns" (vertical gauges) or "rows" (h-bars)
        "margin_x": 6, "margin_top": 6, "margin_bottom": 6, "row_gap": 4,
        "label_width": 48, "percent_width": 34, "bar_radius": 3, "bar_inset": 1,
        "max_rows": 0,  # 0 = no cap; else clamp bars shown
    },
    # Used by the "columns" style: each device is a vertical fill gauge.
    # Colours default to the Okabe-Ito colourblind-safe palette.
    "gauge": {
        "full_color": "#E69F00",   # used / filled portion (orange)
        "empty_color": "#009E73",  # free / empty portion (bluish green)
        "text_color": "#ffffff",
        "font": "", "font_size": 15,
        "show_percent": True, "percent_font_size": 13,
        "name_max": 10,            # truncate device name to this many chars
        # Alternate the vertical label between "slot# name" and "ETA + size".
        "toggle": True,
        "toggle_hz": 0.5,          # 0.5 Hz -> swaps every 2 s
    },
    "label": {"color": "#ffffff", "font": "", "font_size": 14},
    "percent": {"show": True, "color": "#9aa0a6", "font": "", "font_size": 12},
    "bar": {
        "track_color": "#1e1e1e", "fill_color": "#22c35e",
        "border_color": "#3c4043", "border_width": 1,
    },
    "status_colors": {
        "idle": "#3b5bdb", "active": "#22c35e", "done": "#22c35e",
        "error": "#e03131", "paused": "#f08c00",
    },
    "render": {"min_interval_ms": 80},
    "lock": {"path": ""},
    # Optional static slots. Empty by default: the feeder declares slots at
    # runtime via {"slots": [...]}. Provide entries here only for a fixed rig.
    "slots": [],
}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (a copy). Lists are replaced whole."""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None) -> dict:
    if path is None:
        for cand in ("ingest_display.toml",
                     os.path.expanduser("~/.config/sd-card-ingest/display.toml")):
            if os.path.exists(cand):
                path = cand
                break
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, "rb") as fh:
            cfg = deep_merge(DEFAULTS, _load_toml(fh))
    return cfg


def parse_color(s):
    """'#rgb', '#rrggbb', or an [r,g,b] list -> (r, g, b)."""
    if isinstance(s, (list, tuple)):
        return tuple(int(c) for c in s[:3])
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def load_font(path: str, size: int):
    if path:
        return ImageFont.truetype(path, size)
    # Prefer DejaVuSans (has the micro sign); fall back to Pillow's default.
    for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(cand, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

class Slot:
    __slots__ = ("id", "label", "progress", "status", "rate", "eta", "size_bytes")

    def __init__(self, id, label):
        self.id = id
        self.label = label
        self.progress = 0.0
        self.status = "idle"
        self.rate = None        # progress fraction per second, from the feeder
        self.eta = None         # explicit seconds-to-done, if the feeder gives it
        self.size_bytes = None  # total size, for the "NN G" readout

    def effective_eta(self):
        """Seconds to completion: explicit eta, else derived from rate."""
        if self.progress >= 1.0:
            return 0.0
        if self.eta is not None:
            return self.eta
        if self.rate:
            return (1.0 - self.progress) / self.rate
        return None


def _coerce_progress(v):
    v = float(v)
    if v > 1.0:  # accept 0..100 percentages too
        v = v / 100.0
    return max(0.0, min(1.0, v))


def _set_fields(slot: "Slot", obj: dict):
    """Apply the recognised fields of an object update onto a slot."""
    if "label" in obj:
        slot.label = str(obj["label"])
    if "progress" in obj:
        slot.progress = _coerce_progress(obj["progress"])
    if "rate" in obj:
        slot.rate = float(obj["rate"]) if obj["rate"] else None
    if "eta" in obj:
        slot.eta = None if obj["eta"] is None else float(obj["eta"])
    if "size" in obj:  # bytes
        slot.size_bytes = None if obj["size"] is None else float(obj["size"])
    if "gb" in obj:    # convenience: size straight in GB
        slot.size_bytes = None if obj["gb"] is None else float(obj["gb"]) * 1e9
    if "status" in obj:
        slot.status = str(obj["status"])
    elif "progress" in obj:
        slot.status = ("done" if slot.progress >= 1.0
                       else "active" if slot.progress > 0.0 else "idle")


def format_eta(sec) -> str:
    """Compact single-unit ETA: '1.5h', '3m', or '9s' (largest fitting unit)."""
    if sec is None:
        return ""
    sec = int(round(sec))
    if sec <= 0:
        return "0s"
    if sec >= 3600:
        return f"{sec / 3600:.1f}h"
    if sec >= 60:
        return f"{round(sec / 60)}m"
    return f"{sec}s"


def format_gigs(size_bytes) -> str:
    """Compact size, e.g. '238G', '1.0T'."""
    if not size_bytes:
        return ""
    gb = size_bytes / 1e9
    if gb >= 1000:
        return f"{gb / 1000:.1f}T"
    return f"{gb:.0f}G"


class Station:
    """Ordered, dynamic set of slots. Order = the order the feeder gives us."""

    def __init__(self, cfg):
        self.slots = []
        self._by_key = {}
        if cfg["slots"]:
            self.set_slots(cfg["slots"])

    def _reindex(self):
        self._by_key = {}
        for slot in self.slots:
            for key in (slot.id, slot.label, slot.id.lower(), slot.label.lower()):
                self._by_key[key] = slot

    def resolve(self, key):
        return self._by_key.get(key) or self._by_key.get(str(key).lower())

    def set_slots(self, items) -> bool:
        """Replace the ordered slot set, preserving state for surviving ids."""
        old = {s.id: s for s in self.slots}
        new = []
        for it in items:
            if isinstance(it, str):
                sid, label, extra = it, it, {}
            else:
                sid = str(it["id"])
                label = str(it.get("label", sid))
                extra = it
            slot = old.get(sid) or Slot(sid, label)
            slot.label = label
            if isinstance(it, dict):
                _set_fields(slot, it)
            new.append(slot)
        self.slots = new
        self._reindex()
        return True

    def apply(self, obj: dict) -> bool:
        """Apply a JSON update object. Returns True if anything changed."""
        changed = False
        if "slots" in obj:
            changed |= self.set_slots(obj["slots"])
        for key, val in obj.items():
            if key == "slots":
                continue
            slot = self.resolve(key)
            if slot is None:
                print(f"ingest-display: unknown slot {key!r}", file=sys.stderr)
                continue
            changed |= self._apply_one(slot, val)
        return changed

    def _apply_one(self, slot: Slot, val) -> bool:
        if val is None:
            slot.progress, slot.status = 0.0, "idle"
            return True
        if isinstance(val, bool):
            return False
        if isinstance(val, (int, float)):
            slot.progress = _coerce_progress(val)
            slot.status = ("done" if slot.progress >= 1.0
                           else "active" if slot.progress > 0.0 else "idle")
            return True
        if isinstance(val, str):
            slot.status = val
            if val == "done":
                slot.progress = 1.0
            return True
        if isinstance(val, dict):
            had_label = "label" in val
            _set_fields(slot, val)
            if had_label:
                self._reindex()
            return True
        print(f"ingest-display: ignoring value {val!r} for {slot.id}", file=sys.stderr)
        return False


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

class Renderer:
    def __init__(self, cfg):
        self.cfg = cfg
        s = cfg["screen"]
        self.w, self.h, self.rotate = s["width"], s["height"], int(s["rotate"])
        self.bg = parse_color(s["background"])
        self.label_font = load_font(cfg["label"]["font"], cfg["label"]["font_size"])
        self.pct_font = load_font(cfg["percent"]["font"], cfg["percent"]["font_size"])
        self.title_font = load_font(cfg["title"]["font"], cfg["title"]["font_size"])
        self.empty_font = load_font(cfg["empty"]["font"], cfg["empty"]["font_size"])
        g = cfg["gauge"]
        self.gauge_font = load_font(g["font"], g["font_size"])
        self.gauge_pct_font = load_font(g["font"], g["percent_font_size"])

    def render(self, station: Station, phase: int = 0) -> Image.Image:
        cfg = self.cfg
        lay = cfg["layout"]
        if self.rotate in (90, 270):
            cw, ch = self.h, self.w
        else:
            cw, ch = self.w, self.h
        img = Image.new("RGB", (cw, ch), self.bg)
        d = ImageDraw.Draw(img)

        slots = station.slots
        if lay["max_rows"] and len(slots) > lay["max_rows"]:
            slots = slots[:lay["max_rows"]]

        top = lay["margin_top"]
        title = cfg["title"]
        if title["text"] and title["height"] > 0:
            d.text((cw / 2, top + title["height"] / 2), title["text"],
                   font=self.title_font, fill=parse_color(title["color"]),
                   anchor="mm")
            top += title["height"]

        if not slots:
            empty = cfg["empty"]
            d.text((cw / 2, ch / 2), empty["text"], font=self.empty_font,
                   fill=parse_color(empty["color"]), anchor="mm")
        elif lay["style"] == "columns":
            self._draw_columns(img, d, cw, ch, top, slots, phase)
        else:
            self._draw_rows(d, cw, ch, top, slots)

        if self.rotate:
            img = img.rotate(-self.rotate, expand=True)
        return img

    def _column_text(self, i, slot, phase) -> str:
        """The vertical label: 'slot# name' on phase 0, 'ETA · size' on phase 1."""
        g = self.cfg["gauge"]
        name = slot.label
        if len(name) > g["name_max"]:
            name = name[:g["name_max"] - 1] + "…"
        name_text = f"{i + 1} {name}"
        if not (g["toggle"] and phase == 1):
            return name_text
        if slot.progress >= 1.0:
            alt = "done"
        else:
            parts = [format_eta(slot.effective_eta()), format_gigs(slot.size_bytes)]
            alt = " ".join(p for p in parts if p)
        return alt or name_text  # fall back to the name if we have no eta/size

    # -- horizontal bars (label | bar | percent), one row per device -------- #
    def _draw_rows(self, d, cw, ch, top, slots):
        cfg = self.cfg
        lay, bar = cfg["layout"], cfg["bar"]
        bottom = ch - lay["margin_bottom"]
        n = len(slots)
        gap = lay["row_gap"]
        row_h = (bottom - top - (n - 1) * gap) / n

        label_color = parse_color(cfg["label"]["color"])
        pct_cfg = cfg["percent"]
        pct_color = parse_color(pct_cfg["color"])
        pct_w = lay["percent_width"] if pct_cfg["show"] else 0
        track_color = parse_color(bar["track_color"])
        border_color = parse_color(bar["border_color"])
        border_w = bar["border_width"]
        radius = lay["bar_radius"]
        inset = lay["bar_inset"]
        status_colors = {k: parse_color(v) for k, v in cfg["status_colors"].items()}
        default_fill = parse_color(bar["fill_color"])

        bar_x0 = lay["margin_x"] + lay["label_width"]
        bar_x1 = cw - lay["margin_x"] - pct_w
        bar_span = max(0, bar_x1 - bar_x0)

        for i, slot in enumerate(slots):
            y0 = top + i * (row_h + gap)
            y1 = y0 + row_h
            cy = (y0 + y1) / 2
            d.text((lay["margin_x"], cy), slot.label, font=self.label_font,
                   fill=label_color, anchor="lm")
            by0, by1 = y0 + inset, y1 - inset
            d.rounded_rectangle([bar_x0, by0, bar_x1, by1], radius=radius,
                                fill=track_color,
                                outline=border_color if border_w else None,
                                width=border_w)
            fill_w = int(bar_span * slot.progress)
            if fill_w > 0:
                fill_color = status_colors.get(slot.status, default_fill)
                r = min(radius, fill_w // 2)
                d.rounded_rectangle([bar_x0, by0, bar_x0 + fill_w, by1],
                                    radius=r, fill=fill_color)
            if pct_cfg["show"]:
                d.text((cw - lay["margin_x"], cy),
                       f"{int(round(slot.progress * 100))}%",
                       font=self.pct_font, fill=pct_color, anchor="rm")

    # -- vertical gauges: one tall column per device, fill = used space ----- #
    def _draw_columns(self, img, d, cw, ch, top, slots, phase=0):
        cfg = self.cfg
        lay, g = cfg["layout"], cfg["gauge"]
        y0 = top
        y1 = ch - lay["margin_bottom"]
        col_h = y1 - y0
        n = len(slots)
        gap = lay["row_gap"]
        col_w = (cw - 2 * lay["margin_x"] - (n - 1) * gap) / n
        radius = lay["bar_radius"]

        empty_c = parse_color(g["empty_color"])
        full_c = parse_color(g["full_color"])
        text_c = parse_color(g["text_color"])

        for i, slot in enumerate(slots):
            x0 = lay["margin_x"] + i * (col_w + gap)
            x1 = x0 + col_w
            # base column = free space
            d.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=empty_c)
            # used space grows from the bottom
            fill_h = int(col_h * slot.progress)
            if fill_h > 0:
                d.rounded_rectangle([x0, y1 - fill_h, x1, y1],
                                    radius=min(radius, fill_h // 2), fill=full_c)

            text = self._column_text(i, slot, phase)
            self._vtext(img, text, self.gauge_font, text_c,
                        x0, y0, col_w, col_h)

            if g["show_percent"]:
                pct = f"{int(round(slot.progress * 100))}%"
                d.text(((x0 + x1) / 2, y1 - 3), pct, font=self.gauge_pct_font,
                       fill=text_c, anchor="mb")

    def _vtext(self, img, text, font, fill, x0, y0, box_w, box_h):
        """Draw `text` vertically (reading bottom-to-top) centred in a column."""
        tmp = Image.new("RGBA", (int(box_h), int(box_w)), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        # anchor near the top of the column (which is the right end pre-rotation)
        td.text((int(box_h) - 6, int(box_w) / 2), text, font=font, fill=fill + (255,),
                anchor="rm")
        rot = tmp.rotate(90, expand=True)
        img.paste(rot, (int(x0), int(y0)), rot)


# --------------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------------- #

def acquire_lock(cfg):
    path = cfg["lock"]["path"]
    if not path:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        path = os.path.join(runtime, "sd-card-ingest-display.lock")
    fd = open(path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"ingest-display: another instance already owns the display "
                 f"(lock held: {path})")
    fd.write(f"{os.getpid()}\n")
    fd.flush()
    return fd  # keep referenced/open for the process lifetime


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def open_serial(cfg, port_override):
    import serial
    port = port_override or cfg["serial"]["port"]
    if port in ("auto", "", None):
        port = wire.find_port()
    return serial.Serial(port, cfg["serial"]["baud"],
                         timeout=cfg["serial"]["ack_timeout"]), port


def send(ser, renderer, station, ack_timeout, phase=0):
    img = renderer.render(station, phase)
    ser.write(wire.encode_image(img))
    ser.flush()
    wire.read_ack(ser, ack_timeout)


def drain_stdin():
    """Non-blocking read of any further complete lines already buffered."""
    lines = []
    while True:
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            break
        line = sys.stdin.readline()
        if line == "":
            break
        lines.append(line)
    return lines


def feed(station, chunks):
    dirty = False
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            if station.apply(json.loads(chunk)):
                dirty = True
        except json.JSONDecodeError as e:
            print(f"ingest-display: bad JSON: {e}", file=sys.stderr)
    return dirty


def run(cfg, args):
    station = Station(cfg)
    renderer = Renderer(cfg)

    if args.dump:
        feed(station, sys.stdin)
        renderer.render(station, args.phase).save(args.dump)
        print(f"wrote {args.dump}")
        return 0

    lock = acquire_lock(cfg)  # noqa: F841 -- held for lifetime
    ser, port = open_serial(cfg, args.port)
    print(f"ingest-display: driving {port} ({cfg['screen']['width']}x"
          f"{cfg['screen']['height']})", file=sys.stderr)

    interval = cfg["render"]["min_interval_ms"] / 1000.0
    ack_timeout = cfg["serial"]["ack_timeout"]

    # Text-toggle timing (name <-> eta/size). Only relevant for columns style.
    hz = cfg["gauge"]["toggle_hz"]
    toggling = (cfg["gauge"]["toggle"] and cfg["layout"]["style"] == "columns"
                and hz and hz > 0)
    switch = (1.0 / hz) if toggling else None

    def cur_phase():
        return int(time.monotonic() / switch) % 2 if switch else 0

    phase = cur_phase()
    send(ser, renderer, station, ack_timeout, phase)  # initial frame
    last_draw = time.monotonic()
    dirty = False

    while True:
        now = time.monotonic()
        waits = []
        if switch:  # wake at the next toggle boundary
            waits.append((int(now / switch) + 1) * switch - now)
        if dirty:
            waits.append(max(0.0, (last_draw + interval) - now))
        timeout = min(waits) if waits else None

        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            line = sys.stdin.readline()
            if line == "":  # EOF
                break
            if feed(station, [line] + drain_stdin()):
                dirty = True

        now = time.monotonic()
        new_phase = cur_phase()
        if (dirty and now - last_draw >= interval) or new_phase != phase:
            phase = new_phase
            send(ser, renderer, station, ack_timeout, phase)
            last_draw = now
            dirty = False

    if dirty:
        send(ser, renderer, station, ack_timeout, phase)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to a TOML config (default: search)")
    ap.add_argument("--port", help="serial device override (default: from config)")
    ap.add_argument("--dump", metavar="PNG",
                    help="render current+stdin state once to a PNG and exit "
                         "(no board/serial needed)")
    ap.add_argument("--phase", type=int, choices=[0, 1], default=0,
                    help="which toggle phase to render with --dump "
                         "(0 = slot/name, 1 = eta/size)")
    ap.add_argument("--print-config", action="store_true",
                    help="print the merged config as JSON and exit")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.print_config:
        json.dump(cfg, sys.stdout, indent=2)
        print()
        return 0
    return run(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
