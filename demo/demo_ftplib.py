"""
demo/demo_ftplib.py
====================

End-to-end demo that starts the toy FTP server in a background thread
and drives it using Python's *stdlib* ``ftplib.FTP`` client, to prove
the server also speaks real FTP (not just our own toy client).

Run it with:

    python -m demo.demo_ftplib

It will:
  1. start ftp_server.FTPServer on an ephemeral localhost port,
  2. connect with ftplib.FTP and log in,
  3. list the (empty) server root,
  4. upload a local file with STOR,
  5. list again to show it appeared,
  6. download it back with RETR and confirm the bytes match,
  7. QUIT and shut the server down.
"""

from __future__ import annotations

import ftplib
import os
import shutil
import tempfile

from ftp_server.server import FTPServer, DEFAULT_USER, DEFAULT_PASS


def main():
    root_dir = tempfile.mkdtemp(prefix="toy_ftp_demo_root_")
    server = FTPServer(root_dir=root_dir, host="127.0.0.1", port=0)
    server.start()
    print(f"Server started on 127.0.0.1:{server.port}, sandbox root = {root_dir}")

    workdir = tempfile.mkdtemp(prefix="toy_ftp_demo_client_")
    local_upload = os.path.join(workdir, "hello.txt")
    local_download = os.path.join(workdir, "hello_downloaded.txt")
    content = b"Hello from the toy FTP client demo!\nThis line proves STOR/RETR round-trip correctly.\n"
    with open(local_upload, "wb") as fh:
        fh.write(content)

    ftp = ftplib.FTP()
    try:
        print(ftp.connect("127.0.0.1", server.port))
        print(ftp.login(DEFAULT_USER, DEFAULT_PASS))

        print("Directory listing before upload:")
        ftp.retrlines("LIST")

        with open(local_upload, "rb") as fh:
            print(ftp.storbinary("STOR hello.txt", fh))

        print("Directory listing after upload:")
        ftp.retrlines("LIST")

        with open(local_download, "wb") as fh:
            print(ftp.retrbinary("RETR hello.txt", fh.write))

        with open(local_download, "rb") as fh:
            downloaded = fh.read()

        assert downloaded == content, "Downloaded bytes did not match uploaded bytes!"
        print("Round-trip OK: downloaded bytes match uploaded bytes.")

        print(ftp.quit())
    finally:
        try:
            ftp.close()
        except Exception:
            pass
        server.stop()
        shutil.rmtree(root_dir, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
