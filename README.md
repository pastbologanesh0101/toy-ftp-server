# toy-ftp-server

A small, from-scratch FTP server and client built directly on raw TCP
sockets (Python's `socket` module) — no `ftplib`, no third-party
libraries in the server itself. It implements a practical subset of
[RFC 959](https://www.rfc-editor.org/rfc/rfc959), enough to log in,
browse, and move files with any real FTP client (including Python's
own `ftplib`, or `curl`, `lftp`, FileZilla, etc.), while keeping every
operation sandboxed to a single root directory.

## Supported commands

| Command | Description |
|---|---|
| `USER` / `PASS` | Fixed-credential login (default `toyuser` / `toypass`) |
| `PWD` | Print the current virtual working directory |
| `CWD` / `CDUP` | Change directory (rejects any path that escapes the sandbox root) |
| `LIST` / `NLST` | Directory listing, sent over a PASV data connection |
| `RETR` | Download a file |
| `STOR` | Upload a file |
| `DELE` | Delete a file |
| `MKD` | Create a directory |
| `PASV` | Passive-mode data connection setup (the only data-connection mode supported) |
| `TYPE` | Accepts ASCII/binary type requests (transfers are always byte-exact) |
| `SYST` / `FEAT` / `NOOP` | Housekeeping/informational commands |
| `QUIT` | Close the session |

Active mode (`PORT`) is intentionally **not** implemented — PASV is
the practically useful mode and the one every modern client defaults
to.

### Sandboxing

Every path-taking command resolves the requested path against the
server's configured root directory and refuses (`550 Permission
denied`) anything that would resolve outside of it — so `CWD
../../../etc`, `RETR ../../etc/passwd`, etc. all fail safely instead
of touching the real filesystem.

## Project layout

```
ftp_server/     the server: FTPRequestHandler (protocol logic, one do_<CMD>
                method per command) + FTPServer (socket/thread plumbing)
ftp_client/     a small raw-socket FTP client used to prove the server
                works end-to-end without relying on ftplib
demo/           a demo script that drives the server with stdlib ftplib
tests/          unittest-based integration tests (start server, use ftplib)
```

## Running the server

```bash
python -m ftp_server.server --root ./ftp_root --host 127.0.0.1 --port 2121
```

This creates `./ftp_root` if needed and serves it as the sandbox root.
Default credentials are `toyuser` / `toypass` (override with `--user`
and `--password`).

## Example session (stdlib `ftplib`)

```python
import ftplib

ftp = ftplib.FTP()
ftp.connect("127.0.0.1", 2121)
ftp.login("toyuser", "toypass")

ftp.retrlines("LIST")                      # browse the sandbox root
with open("local.txt", "rb") as fh:
    ftp.storbinary("STOR local.txt", fh)   # upload
with open("downloaded.txt", "wb") as fh:
    ftp.retrbinary("RETR local.txt", fh.write)  # download

ftp.quit()
```

Or run the included end-to-end demo, which starts the server itself,
uploads a file, downloads it back, and checks the bytes match:

```bash
python -m demo.demo_ftplib
```

## Example session (included toy client)

```python
from ftp_client import ToyFTPClient

client = ToyFTPClient("127.0.0.1", 2121)
client.connect()
client.login("toyuser", "toypass")
print(client.list())
client.store("local.txt", "remote.txt")
client.retrieve("remote.txt", "downloaded.txt")
client.quit()
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite spins up a real `FTPServer` on an ephemeral port as a
background thread for each test, drives it with Python's stdlib
`ftplib.FTP`, and tears everything down afterwards. It covers:

- successful login with correct credentials
- rejected login with an incorrect password
- `LIST`/`NLST` reflecting an uploaded file
- byte-exact `STOR` → `RETR` round-trip
- `CWD` outside the sandbox root being rejected
- navigating into a subdirectory and back with `CWD`/`CDUP`
- `QUIT` closing the session cleanly
- commands issued before authentication being rejected

CI (`.github/workflows/tests.yml`) runs the same suite on Python 3.11
and 3.12 on every push and pull request.

## License

MIT — see [LICENSE](LICENSE).
