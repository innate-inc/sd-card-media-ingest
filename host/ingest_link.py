"""Serial link + confirm channel.

The device is found by USB VID/PID (its /dev/ttyACM* name isn't stable) using
pyserial. In pipe mode (no vid/pid) the protocol goes to stdout and `confirm`
lines are read from stdin instead. pyserial is imported lazily so pipe/dry-run
mode needs no dependency.
"""
import logging
import re
import termios
import threading
import time

log = logging.getLogger("ingest")


def find_port(vid, pid):
    """First serial device matching VID (and PID, if given). Hex strings like
    '2e8a' / '0x000a'. Returns a /dev path, or None."""
    from serial.tools import list_ports
    want_vid = int(str(vid), 16) if vid else None
    want_pid = int(str(pid), 16) if pid else None
    for p in list_ports.comports():
        if want_vid is not None and p.vid != want_vid:
            continue
        if want_pid is not None and p.pid != want_pid:
            continue
        return p.device
    return None


class SerialLink:
    """A text read/write view of a serial tty: the emitter writes str lines,
    confirm_reader iterates str lines. USB CDC ignores the baud rate."""

    def __init__(self, port):
        import serial
        self.s = serial.Serial(port, timeout=None)

    def write(self, data):
        self._tty(lambda: self.s.write(data.encode("ascii", "replace")))

    def flush(self):
        self._tty(self.s.flush)

    @staticmethod
    def _tty(fn):
        # pyserial lets termios.error (a raw tty I/O failure -- e.g. the board
        # unplugged mid-write) escape; it is NOT an OSError, so translate it so
        # the emitter treats it as a skippable link hiccup instead of crashing.
        try:
            fn()
        except termios.error as e:
            raise OSError(e.args[0] if e.args else 5, "serial tty error") from e

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass

    def __iter__(self):
        while True:
            line = self.s.readline()         # buffered read up to '\n' (blocks)
            if line:
                yield line.decode("ascii", "replace")


class ReconnectingSerial:
    """Display + confirm channel over a USB-CDC serial device (found by VID/PID)
    that transparently reopens after an unplug.

    Writes are best-effort: while the link is down the emitter's frames are
    dropped (copying keeps going) and a reopen is retried at most every
    `reopen_s`. The reader thread re-arms itself when the device returns.
    Connect/disconnect are each logged once, not per frame."""

    def __init__(self, vid, pid, reopen_s=2.0):
        self.vid, self.pid = vid, pid
        self.reopen_s = reopen_s
        self._link = None
        self._lock = threading.Lock()
        self._last_try = 0.0
        self._down = True                    # until the first successful open
        self._reconnected = False            # set on (re)connect; caller re-sends preamble

    def _ensure(self):
        """Return a live SerialLink, or None (retrying no faster than reopen_s)."""
        with self._lock:
            if self._link is not None:
                return self._link
            now = time.monotonic()
            if now - self._last_try < self.reopen_s:
                return None
            self._last_try = now
            port = find_port(self.vid, self.pid)
            if not port:
                return None
            try:
                self._link = SerialLink(port)
            except (OSError, ValueError) as e:
                log.warning("display open failed on %s: %s", port, e)
                return None
            log.info("display connected on %s (%s:%s)", port, self.vid,
                     self.pid or "*")
            self._down = False
            self._reconnected = True
            return self._link

    def _drop(self, why):
        with self._lock:
            if self._link is not None:
                self._link.close()
                self._link = None
            if not self._down:
                self._down = True
                log.warning("display disconnected (%s); retrying every %.0fs",
                            why, self.reopen_s)

    def take_reconnected(self):
        """True once after each (re)connect, so the caller can re-send preamble."""
        with self._lock:
            r = self._reconnected
            self._reconnected = False
            return r

    def write(self, data):
        link = self._ensure()
        if link is not None:
            try:
                link.write(data)
            except OSError as e:
                self._drop(e)

    def flush(self):
        link = self._ensure()
        if link is not None:
            try:
                link.flush()
            except OSError as e:
                self._drop(e)

    def read_confirms(self, q):
        """Thread body: read `confirm <i>` lines, reconnecting across unplugs."""
        while True:
            link = self._ensure()
            if link is None:
                time.sleep(self.reopen_s)
                continue
            try:
                for raw in link:
                    m = re.match(r"\s*confirm\s+(\d+)\s*$", raw)
                    if m:
                        q.put(int(m.group(1)))
            except OSError as e:
                self._drop(e)


def confirm_reader(stream, q):
    """Thread: parse `confirm <i>` lines from the device (or stdin) into a
    queue of slot indices."""
    try:
        for raw in stream:
            m = re.match(r"\s*confirm\s+(\d+)\s*$", raw)
            if m:
                q.put(int(m.group(1)))
    except OSError as e:
        log.warning("confirm channel closed: %s", e)
