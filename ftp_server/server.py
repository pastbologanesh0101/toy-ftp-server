"""
ftp_server.server
==================

A toy FTP server built directly on raw TCP sockets (the ``socket``
module), implementing a practical subset of RFC 959:

    USER, PASS, PWD, CWD, CDUP, LIST, RETR, STOR, PASV, TYPE, SYST,
    NOOP, FEAT, QUIT

Only passive-mode (PASV) data connections are supported; active mode
(PORT) is intentionally left out, as noted as optional in the spec
this project targets.

The protocol logic lives in :class:`FTPRequestHandler`, one instance
per client control connection, with one ``do_<VERB>`` method per FTP
command. That class only depends on a "send a reply line" callback
and a control socket (used for PASV addressing and data-connection
setup) -- it never touches ``threading`` or the listening socket --
so it can be driven directly in tests by calling ``handle_command``
with raw command lines, without needing a full client library.

:class:`FTPServer` is the thin networking wrapper: it listens on a
TCP port and spawns a thread running an :class:`FTPRequestHandler`
per accepted connection.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from datetime import datetime

logger = logging.getLogger("ftp_server")

DEFAULT_USER = "toyuser"
DEFAULT_PASS = "toypass"

_BUFFER_SIZE = 8192


class FTPError(Exception):
    """Raised internally to send a specific FTP error reply."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class FTPRequestHandler:
    """Implements the FTP control-connection protocol for one client.

    Parameters
    ----------
    root_dir:
        The sandbox root directory. No operation may read, write, or
        list anything outside of this directory.
    send_line:
        A callable ``send_line(text: str)`` used to transmit a single
        CRLF-terminated reply line. Kept as an injectable callback
        (rather than baking in socket I/O) so the command dispatch
        logic can be unit tested without a real connection.
    control_sock:
        The raw control-connection socket. Used only for PASV
        addressing (``getsockname``) and to accept data connections;
        may be ``None`` in pure command-dispatch unit tests that never
        exercise LIST/RETR/STOR/PASV.
    username / password:
        The single fixed credential pair this toy server accepts.
        Pass ``username="anonymous"`` and any password to allow
        anonymous-style login.
    """

    def __init__(
        self,
        root_dir,
        send_line,
        control_sock=None,
        username: str = DEFAULT_USER,
        password: str = DEFAULT_PASS,
        peer_name: str = "client",
    ):
        self.root_dir = os.path.realpath(root_dir)
        self._send_line = send_line
        self.control_sock = control_sock
        self.username = username
        self.password = password
        self.peer_name = peer_name

        self.authenticated = False
        self.logged_in_user = None
        self.cwd = "/"  # virtual path, always relative to root_dir
        self.quit_requested = False
        self.type_is_binary = True

        self._data_server_socket = None  # listening socket set up by PASV

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _virtual_to_real(self, virtual_path):
        """Resolve ``virtual_path`` (absolute or relative to ``cwd``)
        to a real filesystem path guaranteed to live inside
        ``root_dir``, plus the resulting normalized virtual path.

        Raises ``FTPError(550, ...)`` if the request would escape the
        sandbox.
        """
        virtual_path = virtual_path.strip() or "."

        if virtual_path.startswith("/"):
            candidate = virtual_path
        else:
            candidate = os.path.join(self.cwd, virtual_path)

        normalized = os.path.normpath(candidate).replace(os.sep, "/")
        if not normalized.startswith("/"):
            normalized = "/" + normalized

        real_path = os.path.realpath(os.path.join(self.root_dir, normalized.lstrip("/")))
        root_with_sep = self.root_dir.rstrip(os.sep) + os.sep

        if not (real_path == self.root_dir or real_path.startswith(root_with_sep)):
            raise FTPError(550, "Permission denied: path escapes sandbox root.")

        return real_path, normalized

    # ------------------------------------------------------------------
    # Reply helper
    # ------------------------------------------------------------------

    def reply(self, code, message):
        self._send_line(f"{code} {message}")

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    #: Commands allowed before authentication completes.
    _UNAUTHENTICATED_OK = frozenset({"USER", "PASS", "QUIT", "FEAT", "NOOP"})

    def handle_command(self, line: str):
        """Parse and dispatch a single raw command line."""
        line = line.rstrip("\r\n")
        if not line:
            return

        if " " in line:
            verb, arg = line.split(" ", 1)
        else:
            verb, arg = line, ""

        verb = verb.upper()

        if verb not in self._UNAUTHENTICATED_OK and not self.authenticated:
            self.reply(530, "Please login with USER and PASS.")
            return

        method = getattr(self, f"do_{verb}", None)
        if method is None:
            self.reply(502, f"Command '{verb}' not implemented.")
            return

        try:
            method(arg)
        except FTPError as exc:
            self.reply(exc.code, exc.message)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unhandled error while processing: %s", line)
            self.reply(451, f"Internal error: {exc}")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def do_USER(self, arg):
        self.logged_in_user = arg.strip()
        self.authenticated = False
        self.reply(331, f"Password required for {self.logged_in_user}.")

    def do_PASS(self, arg):
        if self.logged_in_user is None:
            self.reply(503, "Login with USER first.")
            return

        if self.logged_in_user == self.username and arg == self.password:
            self.authenticated = True
            self.reply(230, "Login successful.")
        else:
            self.authenticated = False
            self.reply(530, "Login incorrect.")

    def do_QUIT(self, arg):
        self.quit_requested = True
        self.reply(221, "Goodbye.")

    def do_FEAT(self, arg):
        self.reply(211, "No features supported.")

    def do_NOOP(self, arg):
        self.reply(200, "NOOP ok.")

    def do_SYST(self, arg):
        self.reply(215, "UNIX Type: L8 (toy-ftp-server)")

    def do_TYPE(self, arg):
        mode = arg.strip().upper()
        if mode.startswith("A"):
            self.type_is_binary = False
            self.reply(200, "Type set to A.")
        elif mode.startswith("I") or mode.startswith("L"):
            self.type_is_binary = True
            self.reply(200, f"Type set to {mode}.")
        else:
            self.reply(504, "Type not supported.")

    # ------------------------------------------------------------------
    # Directory navigation
    # ------------------------------------------------------------------

    def do_PWD(self, arg):
        self.reply(257, f'"{self.cwd}" is the current directory.')

    def do_CWD(self, arg):
        real_path, normalized = self._virtual_to_real(arg)
        if not os.path.isdir(real_path):
            raise FTPError(550, f"{arg}: No such directory.")
        self.cwd = normalized
        self.reply(250, f"Directory changed to {self.cwd}.")

    def do_CDUP(self, arg):
        self.do_CWD("..")

    # ------------------------------------------------------------------
    # Passive-mode data connections
    # ------------------------------------------------------------------

    def do_PASV(self, arg):
        if self.control_sock is None:
            raise FTPError(425, "Passive mode unavailable in this context.")

        if self._data_server_socket is not None:
            try:
                self._data_server_socket.close()
            except OSError:
                pass

        host = self.control_sock.getsockname()[0]
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, 0))
        srv.listen(1)
        srv.settimeout(30)
        self._data_server_socket = srv

        ip, port = srv.getsockname()
        p1, p2 = divmod(port, 256)
        quad = ip.split(".")
        self.reply(
            227,
            "Entering Passive Mode ({}).".format(
                ",".join(quad + [str(p1), str(p2)])
            ),
        )

    def do_PORT(self, arg):
        # Active mode is intentionally unsupported by this toy server.
        raise FTPError(502, "Active mode (PORT) is not supported; use PASV.")

    def _accept_data_connection(self):
        if self._data_server_socket is None:
            raise FTPError(425, "Use PASV first.")
        srv = self._data_server_socket
        self._data_server_socket = None
        try:
            conn, _addr = srv.accept()
        finally:
            try:
                srv.close()
            except OSError:
                pass
        return conn

    # ------------------------------------------------------------------
    # Directory listing
    # ------------------------------------------------------------------

    def do_LIST(self, arg):
        real_path, _ = self._virtual_to_real(arg if arg and not arg.startswith("-") else ".")
        if not os.path.isdir(real_path):
            raise FTPError(550, "Not a directory.")

        data_conn = self._accept_data_connection()
        self.reply(150, "Here comes the directory listing.")
        try:
            lines = []
            for name in sorted(os.listdir(real_path)):
                full = os.path.join(real_path, name)
                lines.append(self._format_list_line(full, name))
            payload = "\r\n".join(lines)
            if payload:
                payload += "\r\n"
            data_conn.sendall(payload.encode("utf-8"))
        finally:
            data_conn.close()
        self.reply(226, "Directory send OK.")

    @staticmethod
    def _format_list_line(full_path, name):
        st = os.stat(full_path)
        is_dir = os.path.isdir(full_path)
        perms = "drwxr-xr-x" if is_dir else "-rw-r--r--"
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%b %d %H:%M")
        return f"{perms} 1 owner group {st.st_size:>10} {mtime} {name}"

    def do_NLST(self, arg):
        real_path, _ = self._virtual_to_real(arg or ".")
        if not os.path.isdir(real_path):
            raise FTPError(550, "Not a directory.")

        data_conn = self._accept_data_connection()
        self.reply(150, "Here comes the name list.")
        try:
            names = sorted(os.listdir(real_path))
            payload = "\r\n".join(names)
            if payload:
                payload += "\r\n"
            data_conn.sendall(payload.encode("utf-8"))
        finally:
            data_conn.close()
        self.reply(226, "Name list send OK.")

    # ------------------------------------------------------------------
    # File transfer
    # ------------------------------------------------------------------

    def do_RETR(self, arg):
        if not arg:
            raise FTPError(501, "RETR requires a filename.")

        real_path, _ = self._virtual_to_real(arg)
        if not os.path.isfile(real_path):
            raise FTPError(550, f"{arg}: No such file.")

        data_conn = self._accept_data_connection()
        self.reply(150, "Opening data connection for transfer.")
        try:
            with open(real_path, "rb") as fh:
                while True:
                    chunk = fh.read(_BUFFER_SIZE)
                    if not chunk:
                        break
                    data_conn.sendall(chunk)
        finally:
            data_conn.close()
        self.reply(226, "Transfer complete.")

    def do_STOR(self, arg):
        if not arg:
            raise FTPError(501, "STOR requires a filename.")

        real_path, _ = self._virtual_to_real(arg)

        data_conn = self._accept_data_connection()
        self.reply(150, "Ready to receive data.")
        try:
            with open(real_path, "wb") as fh:
                while True:
                    chunk = data_conn.recv(_BUFFER_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
        finally:
            data_conn.close()
        self.reply(226, "Transfer complete.")

    def do_DELE(self, arg):
        if not arg:
            raise FTPError(501, "DELE requires a filename.")
        real_path, _ = self._virtual_to_real(arg)
        if not os.path.isfile(real_path):
            raise FTPError(550, f"{arg}: No such file.")
        os.remove(real_path)
        self.reply(250, "File deleted.")

    def do_MKD(self, arg):
        if not arg:
            raise FTPError(501, "MKD requires a directory name.")
        real_path, normalized = self._virtual_to_real(arg)
        if os.path.exists(real_path):
            raise FTPError(550, "Directory already exists.")
        os.mkdir(real_path)
        self.reply(257, f'"{normalized}" directory created.')


class _ClientConnectionThread(threading.Thread):
    """Owns a single accepted control-connection socket end-to-end."""

    def __init__(self, conn, addr, root_dir, username, password):
        super().__init__(daemon=True)
        self.conn = conn
        self.addr = addr
        self.root_dir = root_dir
        self.username = username
        self.password = password

    def _send_line(self, text):
        self.conn.sendall((text + "\r\n").encode("utf-8"))

    def run(self):
        handler = FTPRequestHandler(
            root_dir=self.root_dir,
            send_line=self._send_line,
            control_sock=self.conn,
            username=self.username,
            password=self.password,
            peer_name=f"{self.addr[0]}:{self.addr[1]}",
        )

        try:
            handler.reply(220, "Toy FTP server ready.")
            buffer = b""
            self.conn.settimeout(120)
            while not handler.quit_requested:
                chunk = self.conn.recv(_BUFFER_SIZE)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace")
                    logger.debug("<- %s", line.strip())
                    handler.handle_command(line)
        except (OSError, socket.timeout):
            logger.debug("Connection from %s dropped.", self.addr)
        finally:
            try:
                self.conn.close()
            except OSError:
                pass


class FTPServer:
    """A minimal, threaded FTP server bound to a sandboxed root directory.

    Example
    -------
    >>> server = FTPServer(root_dir="/tmp/ftproot", host="127.0.0.1", port=0)
    >>> server.start()
    >>> server.port  # doctest: +SKIP
    50231
    >>> server.stop()
    """

    def __init__(
        self,
        root_dir,
        host: str = "127.0.0.1",
        port: int = 2121,
        username: str = DEFAULT_USER,
        password: str = DEFAULT_PASS,
    ):
        self.root_dir = os.path.realpath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)
        self.host = host
        self.requested_port = port
        self.username = username
        self.password = password

        self._server_socket = None
        self._accept_thread = None
        self._running = False

    @property
    def port(self):
        if self._server_socket is None:
            raise RuntimeError("Server has not been started yet.")
        return self._server_socket.getsockname()[1]

    def start(self):
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.requested_port))
        self._server_socket.listen(20)
        self._server_socket.settimeout(0.5)

        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        logger.info("Toy FTP server listening on %s:%s (root=%s)", self.host, self.port, self.root_dir)
        return self

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            handler_thread = _ClientConnectionThread(
                conn, addr, self.root_dir, self.username, self.password
            )
            handler_thread.start()

    def stop(self):
        self._running = False
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=5)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run the toy FTP server.")
    parser.add_argument("--root", default="./ftp_root", help="Sandbox root directory to serve.")
    parser.add_argument("--host", default="127.0.0.1", help="Host/IP to bind the control socket to.")
    parser.add_argument("--port", type=int, default=2121, help="Port to bind the control socket to.")
    parser.add_argument("--user", default=DEFAULT_USER, help="Username required to log in.")
    parser.add_argument("--password", default=DEFAULT_PASS, help="Password required to log in.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = FTPServer(
        root_dir=args.root,
        host=args.host,
        port=args.port,
        username=args.user,
        password=args.password,
    )
    server.start()
    print(f"Serving {server.root_dir} on ftp://{args.host}:{server.port} (user={args.user})")
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        print("Shutting down...")
        server.stop()


if __name__ == "__main__":
    main()
