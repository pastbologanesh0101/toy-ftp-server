"""
ftp_client.client
==================

A minimal FTP client implemented directly on raw TCP sockets, written
to talk to :mod:`ftp_server` (or any RFC 959-ish server that supports
PASV). This exists to prove the toy server works end-to-end without
relying on Python's stdlib ``ftplib`` -- see ``demo/demo_ftplib.py``
for a second demonstration that *does* use ``ftplib``, to show the
server also speaks real FTP.
"""

from __future__ import annotations

import re
import socket

_BUFFER_SIZE = 8192
_PASV_RE = re.compile(r"\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)")


class FTPClientError(Exception):
    """Raised when the server returns an unexpected/error reply."""


class ToyFTPClient:
    """A tiny, dependency-free FTP client.

    Example
    -------
        client = ToyFTPClient("127.0.0.1", 2121)
        client.connect()
        client.login("toyuser", "toypass")
        print(client.list())
        client.store("local.txt", "remote.txt")
        client.retrieve("remote.txt", "downloaded.txt")
        client.quit()
    """

    def __init__(self, host: str, port: int = 2121, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buffer = b""

    # ------------------------------------------------------------------
    # Low-level control-channel helpers
    # ------------------------------------------------------------------

    def connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect((self.host, self.port))
        return self._read_reply()

    def _send_command(self, line: str):
        assert self._sock is not None, "Call connect() first."
        self._sock.sendall((line + "\r\n").encode("utf-8"))

    def _read_line(self) -> str:
        assert self._sock is not None
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(_BUFFER_SIZE)
            if not chunk:
                break
            self._buffer += chunk
        if b"\n" not in self._buffer:
            line, self._buffer = self._buffer, b""
        else:
            line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode("utf-8", errors="replace").rstrip("\r")

    def _read_reply(self) -> str:
        """Read a single (non-multiline) reply line, e.g. '230 Login successful.'."""
        line = self._read_line()
        return line

    def _command(self, line: str) -> str:
        self._send_command(line)
        return self._read_reply()

    @staticmethod
    def _code(reply: str) -> int:
        return int(reply[:3])

    def _expect(self, reply: str, *ok_codes: int) -> str:
        if self._code(reply) not in ok_codes:
            raise FTPClientError(f"Unexpected reply: {reply!r} (expected one of {ok_codes})")
        return reply

    # ------------------------------------------------------------------
    # Session commands
    # ------------------------------------------------------------------

    def login(self, username: str, password: str):
        reply = self._command(f"USER {username}")
        if self._code(reply) == 230:
            return reply
        self._expect(reply, 331)
        reply = self._command(f"PASS {password}")
        return self._expect(reply, 230)

    def pwd(self) -> str:
        reply = self._expect(self._command("PWD"), 257)
        match = re.search(r'"([^"]*)"', reply)
        return match.group(1) if match else reply

    def cwd(self, path: str):
        return self._expect(self._command(f"CWD {path}"), 250)

    def _enter_passive_mode(self):
        reply = self._expect(self._command("PASV"), 227)
        match = _PASV_RE.search(reply)
        if not match:
            raise FTPClientError(f"Could not parse PASV reply: {reply!r}")
        nums = [int(n) for n in match.groups()]
        ip = ".".join(str(n) for n in nums[:4])
        port = nums[4] * 256 + nums[5]
        data_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        data_sock.settimeout(self.timeout)
        data_sock.connect((ip, port))
        return data_sock

    def list(self, path: str = "") -> list[str]:
        data_sock = self._enter_passive_mode()
        reply = self._command(f"LIST {path}".strip())
        self._expect(reply, 150)
        chunks = []
        try:
            while True:
                chunk = data_sock.recv(_BUFFER_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            data_sock.close()
        self._expect(self._read_reply(), 226)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        return [line for line in text.split("\r\n") if line]

    def store(self, local_path: str, remote_name: str):
        data_sock = self._enter_passive_mode()
        reply = self._command(f"STOR {remote_name}")
        self._expect(reply, 150)
        try:
            with open(local_path, "rb") as fh:
                while True:
                    chunk = fh.read(_BUFFER_SIZE)
                    if not chunk:
                        break
                    data_sock.sendall(chunk)
        finally:
            data_sock.close()
        return self._expect(self._read_reply(), 226)

    def retrieve(self, remote_name: str, local_path: str):
        data_sock = self._enter_passive_mode()
        reply = self._command(f"RETR {remote_name}")
        self._expect(reply, 150)
        try:
            with open(local_path, "wb") as fh:
                while True:
                    chunk = data_sock.recv(_BUFFER_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
        finally:
            data_sock.close()
        return self._expect(self._read_reply(), 226)

    def quit(self):
        try:
            reply = self._command("QUIT")
        finally:
            if self._sock is not None:
                self._sock.close()
                self._sock = None
        return reply
