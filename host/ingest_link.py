"""Confirm channel (pipe mode).

The station's confirms normally arrive over HTTP (see ingest_web.py). This is
the other source: `confirm <i>` lines on stdin, which keeps
`ingest.py --dry-run` drivable from a shell or a test without a browser.

The USB-CDC serial link that used to carry both the display protocol and the
confirms is gone; the display is a web page now.
"""
import logging
import re

log = logging.getLogger("ingest")


def confirm_reader(stream, q):
    """Thread: parse `confirm <i>` lines from stdin into a queue of slot
    indices. Ends quietly at EOF -- under systemd stdin is /dev/null, so this
    simply retires and HTTP is the only confirm path."""
    try:
        for raw in stream:
            m = re.match(r"\s*confirm\s+(\d+)\s*$", raw)
            if m:
                q.put(int(m.group(1)))
    except OSError as e:
        log.warning("confirm channel closed: %s", e)
