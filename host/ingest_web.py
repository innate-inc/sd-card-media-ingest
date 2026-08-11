"""One-endpoint web display: GET / shows the station, POST / confirms a wipe.

Static HTML and CSS. No JavaScript: the page reloads itself with
`<meta http-equiv="refresh">` and the confirm control is an ordinary form POST,
so it works in any browser, including a text one.

## The confirm is the wipe interlock, and the web changes its character

A physical button confirms whatever is physically in the slot at the moment it
is pressed. A web page confirms what it *displayed*, which may be seconds or
minutes old -- long enough for an operator to pull a finished card and push in
a fresh one, whose data has not been copied anywhere yet.

So a confirm carries the card's UUID as well as its column, and is refused
unless that UUID still matches the card in that column AND that card is still
PENDING. A stale page therefore cannot wipe the card that replaced the one it
was showing. Refusals are logged at WARNING: one means someone nearly erased
the wrong card, which is worth seeing in the journal.

Responses are sent no-store: a cached page is a stale page, and stale pages are
what the UUID check exists to defend against.
"""
import html
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ingest_config import human_bytes

log = logging.getLogger("ingest")

REFRESH_S = 2                      # <meta refresh> period for the status page


def parse_addr(s, default_port=8081):
    """':8081' / '0.0.0.0:8081' / '127.0.0.1:8081' -> (host, port). An empty
    host means all interfaces, matching the [http] addr convention."""
    host, _, port = str(s).rpartition(":")
    try:
        port = int(port)
    except ValueError:
        port = default_port
    return (host or "0.0.0.0", port)


class Station:
    """The live view, written by the daemon each tick and read by handlers.

    A plain list under a lock: publish() swaps in a whole new list rather than
    mutating, so a handler always renders one self-consistent tick.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cards = []
        self._legend = []
        self._bg = "#202020"
        self.wipe_armed = False

    def publish(self, cards):
        with self._lock:
            self._cards = cards

    def configure(self, legend, bg, wipe_armed):
        with self._lock:
            self._legend, self._bg, self.wipe_armed = legend, bg, wipe_armed

    def snapshot(self):
        with self._lock:
            return self._cards, self._legend, self._bg, self.wipe_armed

    def find(self, col, uuid):
        """The card in `col` iff it is still that uuid and still PENDING.
        Both halves matter: the column alone would let a stale page confirm a
        swapped-in card, and PENDING alone would let it confirm mid-copy."""
        with self._lock:
            for c in self._cards:
                if c["col"] == col and c["uuid"] == uuid and c["pending"]:
                    return c
        return None


CSS = """
*{box-sizing:border-box}
body{margin:0;padding:1.5rem;background:%(bg)s;color:#e8e8e8;
     font:15px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
h1{font-size:1rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase;
   margin:0 0 .25rem;color:#9a9a9a}
.sub{color:#6f6f6f;font-size:.8rem;margin-bottom:1.25rem}
.armed{color:#ff6b6b}
.legend{display:flex;flex-wrap:wrap;gap:1rem;margin-bottom:1.5rem;font-size:.8rem;
        color:#9a9a9a}
.legend span{display:flex;align-items:center;gap:.4rem}
.sw{width:.75rem;height:.75rem;border-radius:2px;display:inline-block;
    border:1px solid rgba(255,255,255,.15)}
.card{border:1px solid #303030;border-radius:6px;padding:1rem;margin-bottom:1rem;
      background:rgba(255,255,255,.02)}
.top{display:flex;flex-wrap:wrap;align-items:baseline;gap:.75rem;margin-bottom:.6rem}
.name{font-size:1.05rem;font-weight:600}
.col{color:#6f6f6f;font-size:.8rem}
.badge{font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;
       padding:.15rem .5rem;border-radius:3px;border:1px solid currentColor}
.st-active{color:#f0e442}.st-pending{color:#0072b2}
.st-error{color:#ff6b6b}.st-idle{color:#6f6f6f}
.bar{display:flex;height:1.5rem;border-radius:3px;overflow:hidden;
     background:%(bg)s;border:1px solid #303030}
.bar span{display:block;height:100%%}
.meta{display:flex;flex-wrap:wrap;gap:1.25rem;margin-top:.6rem;font-size:.8rem;
      color:#9a9a9a}
.path{color:#5f5f5f;font-size:.75rem;margin-top:.4rem;word-break:break-all}
form{margin-top:.9rem}
button{font:inherit;font-size:.85rem;padding:.5rem 1rem;border-radius:4px;
       cursor:pointer;background:#2a1616;color:#ff8a8a;border:1px solid #6b2b2b}
button:hover{background:#3a1c1c}
.empty{color:#6f6f6f;padding:2rem 0}
.err{border-color:#6b2b2b;background:rgba(255,107,107,.06)}
a{color:#8ab4f8}
"""


def _page(body, bg, refresh=True):
    meta = ('<meta http-equiv="refresh" content="%d">' % REFRESH_S) if refresh else ""
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            '<meta name=viewport content="width=device-width,initial-scale=1">'
            "%s<title>ingest station</title><style>%s</style></head><body>%s"
            "</body></html>" % (meta, CSS % {"bg": bg}, body))


def _bar(card, bg):
    parts = ['<span style="width:%.1f%%;background:%s"></span>' % (pm / 10.0, col)
             for _name, pm, col in card["segments"]]
    if card["free"] > 0:
        parts.append('<span style="width:%.1f%%;background:%s"></span>'
                     % (card["free"] / 10.0, bg))
    return '<div class="bar">%s</div>' % "".join(parts)


def _card_html(card, bg, numbers=True):
    e = html.escape
    cls = "card err" if card["status"] == "error" else "card"
    out = ['<div class="%s">' % cls,
           '<div class="top"><span class="name">%s</span>' % e(card["label"]),
           '<span class="col">slot %d</span>' % card["col"],
           '<span class="badge st-%s">%s</span></div>'
           % (card["status"], card["status"]),
           _bar(card, bg)]
    # size_mb is decimal MB, as the card advertises it. human_bytes keeps
    # a small card from rounding away to a bare "0 GB".
    meta = ["%s card" % human_bytes(card["size_mb"] * 1_000_000)]
    if numbers:
        for name, pm, _c in card["segments"]:
            meta.append("%s %.1f%%" % (name, pm / 10.0))
    if card["kbps"] > 0:
        meta.append("%.1f MB/s" % (card["kbps"] / 1000.0))
    if card["eta_s"] >= 0:
        meta.append("eta %dm%02ds" % divmod(card["eta_s"], 60))
    out.append('<div class="meta">%s</div>'
               % "".join("<span>%s</span>" % e(m) for m in meta))
    out.append('<div class="path">%s</div>' % e(card["path"]))
    if card["pending"]:
        # The only control on the page, and the only path to deletion. uuid
        # rides along so a stale page cannot confirm a swapped-in card.
        out.append(
            '<form method="post" action="/">'
            '<input type="hidden" name="col" value="%d">'
            '<input type="hidden" name="uuid" value="%s">'
            '<button type="submit">%s</button></form>'
            % (card["col"], e(card["uuid"]),
               "Confirm wipe — erases this card"
               if card["wipe_armed"] else "Confirm (dry run — wipe not armed)"))
    out.append("</div>")
    return "".join(out)


def render(cards, legend, bg, wipe_armed, numbers=True):
    head = ["<h1>ingest station</h1>"]
    head.append('<div class="sub">%s</div>'
                % ('<span class="armed">wipe ARMED — confirming erases the card'
                   "</span>" if wipe_armed else
                   "wipe not armed — confirming only logs what it would delete"))
    head.append('<div class="legend">%s</div>' % "".join(
        '<span><i class="sw" style="background:%s"></i>%s</span>' % (c, html.escape(n))
        for c, n in legend))
    if not cards:
        head.append('<div class="empty">no cards inserted</div>')
    else:
        head.extend(_card_html(c, bg, numbers) for c in cards)
    return _page("".join(head), bg)


class Handler(BaseHTTPRequestHandler):
    server_version = "ingest-station"

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=()):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        # A cached confirm page is a stale confirm page.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        cards, legend, bg, armed = self.server.station.snapshot()
        self._send(200, render(cards, legend, bg, armed,
                               self.server.station_numbers))

    do_HEAD = do_GET

    def do_POST(self):
        if self.path.split("?")[0] != "/":
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        form = urllib.parse.parse_qs(self.rfile.read(min(n, 4096)).decode(
            "utf-8", "replace"))
        col_raw = (form.get("col") or [""])[0]
        uuid = (form.get("uuid") or [""])[0]
        try:
            col = int(col_raw)
        except ValueError:
            col = -1
        card = self.server.station.find(col, uuid)
        if card is None:
            # Loud on purpose: this is a near-miss on a destructive action.
            log.warning("confirm REFUSED: col=%r uuid=%r -- no card in that "
                        "column with that uuid is pending (stale page?)",
                        col_raw, uuid)
            _c, _l, bg, _a = self.server.station.snapshot()
            self._send(409, _page(
                "<h1>confirm refused</h1><div class=\"sub\">That card is no "
                "longer pending in that slot &mdash; it may have been removed, "
                "swapped, or already confirmed. Nothing was wiped.</div>"
                '<p><a href="/">back to the station</a></p>', bg, refresh=False))
            return
        log.info("slot %d: confirm received over http -> wipe (%s)",
                 col, card["label"])
        self.server.confirms.put(col)
        # POST-redirect-GET: without it a browser refresh re-submits the confirm.
        self._send(303, "", "text/plain; charset=utf-8", [("Location", "/")])

    def log_message(self, fmt, *a):
        pass                    # journald already has what matters; no access log


def serve(addr, station, confirms, numbers=True):
    """Start the server on its own daemon thread. Returns it so tests (and a
    caller wanting the bound port) can inspect .server_address."""
    host, port = parse_addr(addr)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.station = station
    srv.confirms = confirms
    srv.station_numbers = numbers
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("web display on http://%s:%d/", host or "0.0.0.0",
             srv.server_address[1])
    return srv
