"""Drive an interactive command (`claude auth login`) in a pty and relay its
output to a chat in ~1 s batches. Stdlib only, no Hermes imports."""
from __future__ import annotations

import fcntl
import os
import re
import select
import struct
import subprocess
import termios
import threading
import time
from typing import Callable, List, Optional

_ESCAPES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC (hyperlinks, titles)
    r"|\x1b\[[0-?]*[ -/]*[@-~]"            # CSI (colour, cursor)
    r"|\x1b[@-Z\\-_]"                      # other two-byte escapes
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPINNER = re.compile(r"^[\s⠀-⣿✻✽✶✳✢·*|/\\-]*$")


def clean(raw: str) -> List[str]:
    """Visible lines of terminal output: escapes stripped, carriage-return
    redraws collapsed to their final state, spinner-only lines dropped."""
    text = _ESCAPES.sub("", raw).replace("\r\n", "\n")
    lines = []
    for line in text.split("\n"):
        if "\r" in line:
            parts = [p for p in line.split("\r") if p.strip() and not _SPINNER.match(p)]
            line = parts[-1] if parts else ""
        line = _CONTROL.sub("", line).rstrip()
        if line and not _SPINNER.match(line):
            lines.append(line)
    return lines


class LoginSession:
    def __init__(
        self,
        argv: List[str],
        env: dict,
        send: Callable[[str], None],
        on_exit: Optional[Callable[[int], None]] = None,
        timeout: float = 600.0,
        batch: float = 1.0,
        cwd: Optional[str] = None,
    ) -> None:
        self.argv, self.env, self.send, self.on_exit = argv, env, send, on_exit
        self.timeout, self.batch, self.cwd = timeout, batch, cwd
        self.proc: Optional[subprocess.Popen] = None
        self.fd = -1
        self.secrets: List[str] = []
        self.sent: List[str] = []
        self.timed_out = False
        self._done = threading.Event()

    def start(self) -> "LoginSession":
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))  # wide: URLs never wrap
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        try:
            self.proc = subprocess.Popen(self.argv, stdin=slave, stdout=slave, stderr=slave, env=self.env, cwd=self.cwd, start_new_session=True, close_fds=True)
        finally:
            os.close(slave)
        self.fd = master
        threading.Thread(target=self._pump, daemon=True).start()
        return self

    def alive(self) -> bool:
        return self.proc is not None and not self._done.is_set()

    def write(self, text: str) -> None:
        text = text.strip()
        if text:
            self.secrets.append(text)
        os.write(self.fd, (text + "\r").encode())

    def cancel(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done.wait(timeout)

    def _redact(self, line: str) -> str:
        for s in self.secrets:
            if len(s) >= 4:
                line = line.replace(s, "•" * 6)
        return line

    def _flush(self, buf: str) -> None:
        out = []
        for line in clean(buf):
            line = self._redact(line)
            if line in self.sent[-20:]:
                continue
            self.sent.append(line)
            out.append(line)
        if out:
            try:
                self.send("\n".join(out))
            except Exception:
                pass

    def _pump(self) -> None:
        assert self.proc is not None
        deadline = time.monotonic() + self.timeout
        buf, last_data, buf_start = "", 0.0, 0.0
        decoder_tail = b""
        while True:
            if time.monotonic() > deadline:
                self.timed_out = True
                self.cancel()
            try:
                r, _, _ = select.select([self.fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                if not buf:
                    buf_start = time.monotonic()
                data = decoder_tail + data
                try:
                    buf += data.decode("utf-8")
                    decoder_tail = b""
                except UnicodeDecodeError as e:
                    buf += data[:e.start].decode("utf-8", "replace")
                    decoder_tail = data[e.start:]
                last_data = time.monotonic()
            now = time.monotonic()
            if buf and (now - last_data >= self.batch or now - buf_start >= 3 * self.batch):
                self._flush(buf)
                buf = ""
            if not r and self.proc.poll() is not None:
                break
        if buf:
            self._flush(buf)
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            rc = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            rc = -9
        self._done.set()
        if self.on_exit:
            try:
                self.on_exit(rc)
            except Exception:
                pass
