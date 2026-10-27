"""Kanvas agents (jaimarad/Kanvas fork): terminal sessions that the board starts and the browser attaches to.

A session is a program, such as a shell or an agent, running in a pseudo-terminal on the computer that runs
Kanvas. Its output is kept, so a browser that attaches later, or again after a reload, sees the screen as it
was; any number of browsers can attach at once and type into it. Sessions outlive the browser: closing the
tab leaves them running until the program ends or someone stops it.

The browser talks to a session over a WebSocket, implemented here on top of http.server so Kanvas still
needs only the standard library. Binary frames carry the terminal's bytes both ways; text frames carry
JSON control messages: {"t": "r", "c": cols, "r": rows} from the browser to resize, and
{"t": "exit", "code": n} to the browser when the program ends.

Linux-first: this needs a POSIX pty, so kanvas.py loads it only where os.name is "posix".
"""

import base64
import hashlib
import json
import os
import queue
import secrets
import signal
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import fcntl
import termios

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(HERE, "vendor")
VENDOR_FILES = {
    "xterm.js": ("xterm/xterm.js", "text/javascript; charset=utf-8"),
    "xterm.css": ("xterm/xterm.css", "text/css; charset=utf-8"),
    "addon-fit.js": ("xterm/addon-fit.js", "text/javascript; charset=utf-8"),
}

SCROLLBACK = 512 * 1024  # bytes of output kept per session, replayed to a browser that attaches
MAX_RUNNING = 16
MAX_FRAME = 1024 * 1024
STOP_GRACE = 3.0  # seconds between SIGTERM and SIGKILL
PING_EVERY = 30.0  # seconds; finds browsers that went away without closing
CLIENT_BACKLOG = 4096  # chunks queued for one browser before it counts as stuck

# Run in the child before the program: the pty becomes its controlling terminal, so it gets SIGWINCH on
# resize and job control works. Popen's start_new_session has already made it a session leader.
_CTTY = (
    "import fcntl, os, sys, termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "try:\n"
    "    os.execvp(sys.argv[1], sys.argv[1:])\n"
    "except OSError as e:\n"
    "    sys.stderr.write(f'kanvas: cannot run {sys.argv[1]}: {e.strerror}\\r\\n')\n"
    "    sys.exit(127)\n"
)


def _shell():
    return [os.environ.get("SHELL") or "/bin/bash", "-l"]


# What each role runs, given the session. Milestone 2 has a plain shell; the agents' roles come next.
ROLES = {
    "shell": {"title": "Terminal", "argv": lambda s: _shell()},
}


class SessionError(Exception):
    pass


class Session:
    def __init__(self, board, task, role, argv, cwd, env=None):
        self.id = secrets.token_hex(4)
        self.board = board
        self.task = task
        self.role = role
        self.argv = list(argv)
        self.cwd = cwd
        self.started = time.time()
        self.ended = None
        self.exit_code = None
        self.lock = threading.Lock()
        self.clients = set()
        self.buf = bytearray()
        self.env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", KANVAS_BOARD=board,
                        KANVAS_SESSION=self.id, **({"KANVAS_TASK": task} if task else {}), **(env or {}))
        self.proc = None
        self.fd = None

    def start(self, cols=80, rows=24):
        master, slave = os.openpty()
        try:
            _set_size(slave, cols, rows)
            self.proc = subprocess.Popen([sys.executable, "-c", _CTTY] + self.argv, stdin=slave, stdout=slave,
                                         stderr=slave, cwd=self.cwd, env=self.env, start_new_session=True,
                                         close_fds=True)
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.fd = master
        threading.Thread(target=self._pump, name=f"session-{self.id}", daemon=True).start()
        return self

    @property
    def running(self):
        return self.ended is None

    def _pump(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:  # EIO once the program and everything it started have closed the terminal
                break
            if not data:
                break
            with self.lock:
                self.buf += data
                if len(self.buf) > SCROLLBACK:
                    del self.buf[:len(self.buf) - SCROLLBACK]
                clients = list(self.clients)
            for c in clients:
                c.send_binary(data)
        code = self.proc.wait()
        with self.lock:
            self.exit_code = code
            self.ended = time.time()
            clients = list(self.clients)
        os.close(self.fd)
        for c in clients:
            c.send_text(self._exit_message())

    def _exit_message(self):
        return json.dumps({"t": "exit", "code": self.exit_code})

    def write(self, data):
        if not self.running:
            return
        try:
            while data:
                n = os.write(self.fd, data)
                data = data[n:]
        except OSError:
            pass

    def resize(self, cols, rows):
        if self.running and 0 < cols < 1000 and 0 < rows < 1000:
            try:
                _set_size(self.fd, cols, rows)
            except OSError:
                pass

    def attach(self, client):
        """Replays the kept output to `client`, then streams to it; all under the lock, so nothing is missed or doubled."""
        with self.lock:
            if self.buf:
                client.send_binary(bytes(self.buf))
            if not self.running:
                client.send_text(self._exit_message())
            self.clients.add(client)

    def detach(self, client):
        with self.lock:
            self.clients.discard(client)

    def stop(self):
        """Ends the program and everything it started, as closing a terminal window does: SIGHUP, which interactive
        shells and Claude Code obey, and SIGTERM; then SIGKILL if it is still there after a while."""
        if not self.running:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGHUP)
            os.killpg(self.proc.pid, signal.SIGTERM)
        except OSError:
            return

        def kill():
            if self.running:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        t = threading.Timer(STOP_GRACE, kill)
        t.daemon = True
        t.start()

    def info(self):
        return {"id": self.id, "task": self.task, "role": self.role, "title": ROLES.get(self.role, {}).get("title", self.role),
                "running": self.running, "exit": self.exit_code, "started": _iso(self.started),
                "ended": _iso(self.ended) if self.ended else None}


def _set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Sessions:
    """Every session this Kanvas runs, for all its boards."""

    def __init__(self, roles=None):
        self.roles = roles or ROLES
        self.items = {}  # id -> Session, oldest first
        self.lock = threading.Lock()

    def start(self, board, task=None, role="shell", cols=80, rows=24):
        spec = self.roles.get(role)
        if not spec:
            raise SessionError(f"Unknown role {role!r}; expected one of {', '.join(self.roles)}")
        with self.lock:
            if sum(s.running for s in self.items.values()) >= MAX_RUNNING:
                raise SessionError(f"{MAX_RUNNING} sessions are already running; stop one first")
            s = Session(board, task, role, [], os.path.dirname(board))
            s.argv = list(spec["argv"](s))
            s.start(cols, rows)
            self.items[s.id] = s
        return s

    def get(self, sid, board=None):
        with self.lock:
            s = self.items.get(sid)
        return s if s and (board is None or s.board == board) else None

    def list(self, board):
        with self.lock:
            return [s for s in self.items.values() if s.board == board]

    def remove(self, sid):
        with self.lock:
            s = self.items.pop(sid, None)
        if s:
            s.stop()
        return s

    def stop_all(self):
        with self.lock:
            items = list(self.items.values())
        for s in items:
            s.stop()


# ---------------------------------------------------------------------------
# WebSocket (RFC 6455), server side, on a BaseHTTPRequestHandler's connection
# ---------------------------------------------------------------------------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WsClosed(Exception):
    pass


def ws_accept_key(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")


def ws_frame(opcode, payload, mask=None):
    """One unfragmented frame; `mask` (4 bytes) only for frames a client sends."""
    n = len(payload)
    head = bytes([0x80 | opcode])
    bit = 0x80 if mask else 0
    if n < 126:
        head += bytes([bit | n])
    elif n < 1 << 16:
        head += bytes([bit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([bit | 127]) + struct.pack("!Q", n)
    if mask:
        return head + mask + _unmask(payload, mask)
    return head + payload


def _unmask(data, mask):
    n = len(data)
    if not n:
        return b""
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


def ws_read_frame(rfile):
    """(fin, opcode, payload) of the next frame, unmasked."""
    head = _read_exact(rfile, 2)
    fin, opcode = head[0] & 0x80, head[0] & 0x0F
    masked, n = head[1] & 0x80, head[1] & 0x7F
    if n == 126:
        n = struct.unpack("!H", _read_exact(rfile, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _read_exact(rfile, 8))[0]
    if n > MAX_FRAME:
        raise WsClosed("frame too large")
    mask = _read_exact(rfile, 4) if masked else None
    payload = _read_exact(rfile, n)
    return bool(fin), opcode, _unmask(payload, mask) if mask else payload


def _read_exact(rfile, n):
    data = rfile.read(n) if n else b""
    if len(data) < n:
        raise WsClosed("connection closed")
    return data


class WebSocket:
    """A browser's connection: frames go out through a queue and a writer thread, so a slow browser holds up
    only itself, never the session or the other browsers."""

    def __init__(self, sock, rfile):
        self.sock = sock
        self.rfile = rfile
        self.out = queue.Queue(CLIENT_BACKLOG)
        self.closed = threading.Event()
        threading.Thread(target=self._write, daemon=True).start()

    def send_binary(self, data):
        self._put(ws_frame(OP_BINARY, data))

    def send_text(self, text):
        self._put(ws_frame(OP_TEXT, text.encode("utf-8")))

    def _put(self, frame):
        if self.closed.is_set():
            return
        try:
            self.out.put_nowait(frame)
        except queue.Full:  # the browser stopped reading
            self.close()

    def _write(self):
        while not self.closed.is_set():
            try:
                frame = self.out.get(timeout=PING_EVERY)
            except queue.Empty:
                frame = ws_frame(OP_PING, b"")
            if frame is None:
                break
            try:
                self.sock.sendall(frame)
            except OSError:
                break
        self.close()

    def messages(self):
        """Yields (opcode, payload) for each data message until the browser closes; answers pings itself."""
        parts, first = [], None
        while not self.closed.is_set():
            try:
                fin, op, payload = ws_read_frame(self.rfile)
            except (WsClosed, OSError, ValueError):
                break
            if op == OP_CLOSE:
                self._put(ws_frame(OP_CLOSE, payload[:2]))
                break
            if op == OP_PING:
                self._put(ws_frame(OP_PONG, payload))
                continue
            if op == OP_PONG:
                continue
            if op != OP_CONT:
                parts, first = [], op
            parts.append(payload)
            if sum(map(len, parts)) > MAX_FRAME:
                break
            if fin and first is not None:
                yield first, b"".join(parts)
                parts, first = [], None
        self.close()

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self.out.put_nowait(None)
        except queue.Full:
            pass
        # Flush what is queued (such as the close frame) before the socket goes.
        def finish():
            time.sleep(0.2)
            try:
                self.sock.shutdown(2)
            except OSError:
                pass
        threading.Thread(target=finish, daemon=True).start()


def _origin_ok(handler):
    """Browsers send Origin with every WebSocket, and do not apply the same-origin policy to them, so another
    site could otherwise open a terminal here. Only the board's own page, from the same host, may."""
    origin = handler.headers.get("Origin")
    if not origin:
        return False
    host = (handler.headers.get("Host") or "").lower()
    return urlparse(origin).netloc.lower() == host and host != ""


# ---------------------------------------------------------------------------
# HTTP routes, called from kanvas.py's BoardHandler
# ---------------------------------------------------------------------------


def handle_vendor(handler, name):
    entry = VENDOR_FILES.get(name)
    if not entry:
        return handler._error(404, "Not found")
    try:
        with open(os.path.join(VENDOR_DIR, entry[0]), "rb") as f:
            body = f.read()
    except OSError:
        return handler._error(404, "Missing vendored file " + entry[0])
    handler._send(200, body, entry[1])


def handle(handler, method, board, rest, sessions):
    """Answers /<board>/api/sessions[/...]; `rest` is what follows "sessions".

    GET lists the board's sessions, POST starts one, POST <id>/stop ends its program but keeps its output,
    DELETE <id> ends it and forgets it, and GET <id>/socket attaches over a WebSocket."""
    if method == "GET" and rest in ([], [""]):
        body = {"sessions": [s.info() for s in sessions.list(board)], "roles": {k: v["title"] for k, v in sessions.roles.items()}}
        return handler._send(200, json.dumps(body).encode("utf-8"))
    if method == "POST" and rest in ([], [""]):
        text = handler._read_json()
        if text is None:
            return
        try:
            req = json.loads(text or "{}")
            task = req.get("task")
            if task is not None and not isinstance(task, str):
                raise SessionError("Expected the task's ID")
            s = sessions.start(board, task, req.get("role", "shell"), int(req.get("cols") or 80), int(req.get("rows") or 24))
        except (SessionError, ValueError, TypeError, AttributeError) as e:
            return handler._error(400, str(e))
        except OSError as e:
            return handler._error(500, f"Could not start the session: {e}")
        return handler._send(200, json.dumps(s.info()).encode("utf-8"))
    s = sessions.get(rest[0], board) if rest else None
    if s is None:
        return handler._error(404, "No such session")
    if method == "POST" and rest[1:] == ["stop"]:
        if handler._read_json() is None:
            return
        s.stop()
        return handler._send(200, json.dumps(s.info()).encode("utf-8"))
    if method == "DELETE" and len(rest) == 1:
        sessions.remove(s.id)
        return handler._send(200, json.dumps(s.info()).encode("utf-8"))
    if method == "GET" and rest[1:] == ["socket"]:
        return _socket(handler, s)
    return handler._error(404, "Not found")


def _socket(handler, session):
    h = handler.headers
    if (h.get("Upgrade") or "").lower() != "websocket" or not h.get("Sec-WebSocket-Key"):
        return handler._error(400, "Expected a WebSocket")
    if not _origin_ok(handler):
        return handler._error(403, "Terminals open only from the board's own page")
    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", ws_accept_key(h["Sec-WebSocket-Key"].strip()))
    handler.end_headers()
    handler.wfile.flush()
    handler.close_connection = True
    ws = WebSocket(handler.connection, handler.rfile)
    session.attach(ws)
    try:
        for op, payload in ws.messages():
            if op == OP_BINARY:
                session.write(payload)
            elif op == OP_TEXT:
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("t") == "r":
                    try:
                        session.resize(int(msg["c"]), int(msg["r"]))
                    except (KeyError, TypeError, ValueError):
                        pass
    finally:
        session.detach(ws)
        ws.close()
