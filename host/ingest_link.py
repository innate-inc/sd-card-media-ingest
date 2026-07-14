"""Serial link + confirm channel.

The device is found by USB VID/PID (its /dev/ttyACM* name isn't stable) using
pyserial. In pipe mode (no vid/pid) the protocol goes to stdout and `confirm`
lines are read from stdin instead. pyserial is imported lazily so pipe/dry-run
mode needs no dependency.
"""
import logging
import re
import termios

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

    def __iter__(self):
        while True:
            line = self.s.readline()         # buffered read up to '\n' (blocks)
            if line:
                yield line.decode("ascii", "replace")


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
