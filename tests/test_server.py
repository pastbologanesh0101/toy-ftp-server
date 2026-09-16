"""
Integration tests for ftp_server.FTPServer.

Each test starts a fresh FTPServer instance (background accept thread)
on an ephemeral localhost port with an isolated temp-directory sandbox
root, drives it with Python's stdlib ftplib.FTP client, and tears both
down afterwards -- closing the client connection and stopping the
server thread -- so nothing lingers between tests.
"""

from __future__ import annotations

import ftplib
import os
import shutil
import tempfile
import unittest

from ftp_server.server import DEFAULT_PASS, DEFAULT_USER, FTPServer


class FTPServerTestCase(unittest.TestCase):
    def setUp(self):
        self.root_dir = tempfile.mkdtemp(prefix="toy_ftp_test_root_")
        self.server = FTPServer(root_dir=self.root_dir, host="127.0.0.1", port=0)
        self.server.start()
        self.client = None

    def tearDown(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.server.stop()
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def _connected_client(self):
        client = ftplib.FTP()
        client.connect("127.0.0.1", self.server.port, timeout=10)
        self.client = client
        return client

    # ------------------------------------------------------------------

    def test_login_succeeds_with_correct_credentials(self):
        client = self._connected_client()
        reply = client.login(DEFAULT_USER, DEFAULT_PASS)
        self.assertTrue(reply.startswith("230"))

    def test_login_rejected_with_wrong_password(self):
        client = self._connected_client()
        with self.assertRaises(ftplib.error_perm) as ctx:
            client.login(DEFAULT_USER, "definitely-not-the-password")
        self.assertTrue(str(ctx.exception).startswith("530"))

    def test_list_shows_uploaded_file(self):
        client = self._connected_client()
        client.login(DEFAULT_USER, DEFAULT_PASS)

        client.storbinary("STOR listed.txt", _bytes_io(b"content for listing test"))

        names = client.nlst()
        self.assertIn("listed.txt", names)

    def test_stor_then_retr_roundtrips_bytes_exactly(self):
        client = self._connected_client()
        client.login(DEFAULT_USER, DEFAULT_PASS)

        payload = os.urandom(4096) + b"toy-ftp-server round trip marker"
        client.storbinary("STOR roundtrip.bin", _bytes_io(payload))

        downloaded = bytearray()
        client.retrbinary("RETR roundtrip.bin", downloaded.extend)

        self.assertEqual(bytes(downloaded), payload)

    def test_cwd_outside_sandbox_root_is_rejected(self):
        client = self._connected_client()
        client.login(DEFAULT_USER, DEFAULT_PASS)

        with self.assertRaises(ftplib.error_perm) as ctx:
            client.cwd("../../../../etc")
        self.assertTrue(str(ctx.exception).startswith("550"))

    def test_cwd_into_subdirectory_then_back_up_works(self):
        os.mkdir(os.path.join(self.root_dir, "sub"))
        client = self._connected_client()
        client.login(DEFAULT_USER, DEFAULT_PASS)

        client.cwd("sub")
        self.assertEqual(client.pwd(), "/sub")
        client.cwd("..")
        self.assertEqual(client.pwd(), "/")

    def test_quit_closes_connection_cleanly(self):
        client = self._connected_client()
        client.login(DEFAULT_USER, DEFAULT_PASS)
        reply = client.quit()
        self.assertTrue(reply.startswith("221"))
        self.client = None  # already closed by quit()

    def test_unauthenticated_command_is_rejected(self):
        client = self._connected_client()
        with self.assertRaises(ftplib.error_perm) as ctx:
            client.nlst()
        self.assertTrue(str(ctx.exception).startswith("530"))


def _bytes_io(data: bytes):
    import io

    return io.BytesIO(data)


if __name__ == "__main__":
    unittest.main()
