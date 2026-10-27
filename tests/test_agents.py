"""Tests for kanvas_agents.py, the fork's terminal sessions. Run with: python -m unittest discover -s tests -v

They need a POSIX pty, so they skip on Windows; run them in Docker (see docker-compose.yml).
"""

import atexit
import base64
import http.client
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import kanvas  # noqa: E402

os.environ.pop("KANVAS_BOARD", None)
STATE_DIR = tempfile.mkdtemp(prefix="kanvas-state-")
os.environ["KANVAS_STATE_DIR"] = STATE_DIR
atexit.register(shutil.rmtree, STATE_DIR, True)

agents = kanvas.AGENTS
needs_pty = unittest.skipUnless(agents, "terminal sessions need a POSIX pty")


class FakeClient:
    """Collects what a session sends to one browser."""

    def __init__(self):
        self.data = bytearray()
        self.texts = []
        self.got = threading.Condition()

    def send_binary(self, data):
        with self.got:
            self.data += data
            self.got.notify_all()

    def send_text(self, text):
        with self.got:
            self.texts.append(json.loads(text))
            self.got.notify_all()

    def wait_for(self, check, timeout=10):
        with self.got:
            if not self.got.wait_for(lambda: check(self), timeout):
                raise AssertionError(f"timed out; output so far: {bytes(self.data)!r}, messages: {self.texts}")


def sh(script):
    return {"t": {"title": "Test", "argv": lambda s: ["sh", "-c", script]}}


@needs_pty
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.board = os.path.join(self.dir, "kanvas.json")

    def start(self, script, **kw):
        sessions = agents.Sessions(sh(script))
        self.addCleanup(sessions.stop_all)
        return sessions, sessions.start(self.board, "TR-01", "t", **kw)

    def test_output_input_and_exit_code(self):
        _, s = self.start('echo "hello $KANVAS_TASK"; read x; echo "got $x"; exit 3')
        c = FakeClient()
        s.attach(c)
        c.wait_for(lambda c: b"hello TR-01" in c.data)
        s.write(b"abc\n")
        c.wait_for(lambda c: c.texts)
        self.assertIn(b"got abc", c.data)
        self.assertEqual(c.texts, [{"t": "exit", "code": 3}])
        self.assertFalse(s.running)
        self.assertEqual(s.info()["exit"], 3)

    def test_runs_in_the_boards_folder_on_a_terminal(self):
        _, s = self.start("pwd; tty; sleep 5")
        c = FakeClient()
        s.attach(c)
        c.wait_for(lambda c: b"/dev/pts/" in c.data)
        self.assertIn(os.path.realpath(self.dir).encode(), c.data)

    def test_resize_reaches_the_program(self):
        # The pty is the program's controlling terminal, so it gets SIGWINCH and sees the new size.
        _, s = self.start("trap 'stty size' WINCH; echo ready; while :; do sleep 0.1; done", cols=80, rows=24)
        c = FakeClient()
        s.attach(c)
        c.wait_for(lambda c: b"ready" in c.data)
        s.resize(132, 40)
        c.wait_for(lambda c: b"40 132" in c.data)

    def test_a_later_browser_sees_the_earlier_output(self):
        _, s = self.start("echo first; sleep 5")
        a = FakeClient()
        s.attach(a)
        a.wait_for(lambda c: b"first" in c.data)
        b = FakeClient()
        s.attach(b)
        self.assertIn(b"first", b.data)

    def test_attaching_after_the_end_gives_the_exit(self):
        _, s = self.start("echo bye")
        deadline = time.time() + 10
        while s.running and time.time() < deadline:
            time.sleep(0.05)
        c = FakeClient()
        s.attach(c)
        self.assertIn(b"bye", c.data)
        self.assertEqual(c.texts, [{"t": "exit", "code": 0}])

    def test_stop_ends_what_the_program_started(self):
        marker = os.path.join(self.dir, "child.pid")
        sessions, s = self.start(f"sleep 60 & echo $! > {marker}; wait")
        deadline = time.time() + 10
        while not os.path.exists(marker) and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.1)
        with open(marker) as f:
            child = int(f.read())
        sessions.remove(s.id)
        while s.running and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(s.running)
        with self.assertRaises(OSError):
            os.kill(child, 0)
        self.assertIsNone(sessions.get(s.id))

    def test_stop_ends_a_program_that_ignores_sigterm_at_once(self):
        # Interactive shells ignore SIGTERM; they end on SIGHUP, as when a terminal window closes.
        sessions, s = self.start("trap '' TERM; echo ready; while :; do sleep 0.1; done")
        c = FakeClient()
        s.attach(c)
        c.wait_for(lambda c: b"ready" in c.data)
        sessions.remove(s.id)
        c.wait_for(lambda c: c.texts, timeout=agents.STOP_GRACE - 1)
        self.assertEqual(c.texts[-1]["code"], -1)  # SIGHUP

    def test_a_missing_program_says_so(self):
        sessions = agents.Sessions({"t": {"title": "T", "argv": lambda s: ["no-such-program-kanvas"]}})
        s = sessions.start(self.board, None, "t")
        c = FakeClient()
        s.attach(c)
        c.wait_for(lambda c: c.texts)
        self.assertIn(b"cannot run no-such-program-kanvas", c.data)
        self.assertEqual(c.texts[-1]["code"], 127)

    def test_unknown_role(self):
        with self.assertRaises(agents.SessionError):
            agents.Sessions(sh("true")).start(self.board, None, "nope")


class FrameTests(unittest.TestCase):
    @needs_pty
    def test_round_trip_at_every_length_encoding(self):
        for n in (0, 5, 125, 126, 65535, 65536, 200000):
            payload = os.urandom(n)
            frame = agents.ws_frame(agents.OP_BINARY, payload, mask=b"\x01\x02\x03\x04")
            self.assertEqual(agents.ws_read_frame(io.BytesIO(frame)), (True, agents.OP_BINARY, payload))

    @needs_pty
    def test_accept_key_from_the_rfc(self):
        self.assertEqual(agents.ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


class WsClient:
    """Just enough of a browser's WebSocket for the tests."""

    def __init__(self, port, path, origin=None, host=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        host = host or f"127.0.0.1:{port}"
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        if origin is not False:
            lines.append(f"Origin: {origin or 'http://' + host}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.rfile = self.sock.makefile("rb")
        self.status = int(self.rfile.readline().split()[1])
        while self.rfile.readline() not in (b"\r\n", b""):
            pass
        self.data = bytearray()
        self.texts = []

    def send(self, data, text=False):
        op = agents.OP_TEXT if text else agents.OP_BINARY
        self.sock.sendall(agents.ws_frame(op, data.encode() if text else data, mask=os.urandom(4)))

    def read_until(self, check, timeout=10):
        deadline = time.time() + timeout
        while not check(self):
            self.sock.settimeout(max(0.1, deadline - time.time()))
            _, op, payload = agents.ws_read_frame(self.rfile)
            if op == agents.OP_BINARY:
                self.data += payload
            elif op == agents.OP_TEXT:
                self.texts.append(json.loads(payload))
            if time.time() > deadline:
                raise AssertionError(f"timed out; got {bytes(self.data)!r}")

    def close(self):
        self.sock.close()


@needs_pty
class SessionServerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "proj", "kanvas.json")
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write(kanvas.dumps(kanvas.new_board("Test")))
        self.server = kanvas.start_server(self.path, "127.0.0.1", 0, quiet=True)
        self.server.sessions.roles = sh('echo "ready"; while read line; do echo "said $line"; done')
        self.port = self.server.server_address[1]
        self.base = "/proj/api/sessions"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Content-Type": ctype} if ctype else {}
            conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
            r = conn.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            conn.close()

    def test_start_attach_type_and_stop(self):
        code, s = self.request("POST", self.base, {"role": "t", "task": "TR-01"})
        self.assertEqual(code, 200, s)
        self.assertTrue(s["running"])
        code, listed = self.request("GET", self.base)
        self.assertEqual([x["id"] for x in listed["sessions"]], [s["id"]])
        ws = WsClient(self.port, f"{self.base}/{s['id']}/socket")
        self.addCleanup(ws.close)
        self.assertEqual(ws.status, 101)
        ws.read_until(lambda c: b"ready" in c.data)
        ws.send(json.dumps({"t": "r", "c": 100, "r": 30}), text=True)
        ws.send(b"hi\r")
        ws.read_until(lambda c: b"said hi" in c.data)
        # Stopping ends the program and keeps the session, with its output, until it is removed.
        code, _ = self.request("POST", f"{self.base}/{s['id']}/stop", {})
        self.assertEqual(code, 200)
        ws.read_until(lambda c: c.texts)
        self.assertEqual(ws.texts[-1]["t"], "exit")
        [listed] = self.request("GET", self.base)[1]["sessions"]
        self.assertFalse(listed["running"])
        later = WsClient(self.port, f"{self.base}/{s['id']}/socket")
        self.addCleanup(later.close)
        later.read_until(lambda c: c.texts)
        self.assertIn(b"said hi", later.data)
        self.assertEqual(self.request("DELETE", f"{self.base}/{s['id']}")[0], 200)
        self.assertEqual(self.request("GET", self.base)[1]["sessions"], [])

    def test_other_sites_cannot_open_a_terminal(self):
        _, s = self.request("POST", self.base, {"role": "t"})
        for origin in ("http://evil.example", False):
            ws = WsClient(self.port, f"{self.base}/{s['id']}/socket", origin=origin)
            self.addCleanup(ws.close)
            self.assertEqual(ws.status, 403, origin)

    def test_starting_and_stopping_need_json(self):
        # A form post from another site cannot send application/json without a CORS preflight.
        self.assertEqual(self.request("POST", self.base, {"role": "t"}, ctype="text/plain")[0], 415)
        _, s = self.request("POST", self.base, {"role": "t"})
        self.assertEqual(self.request("POST", f"{self.base}/{s['id']}/stop", {}, ctype="text/plain")[0], 415)
        self.assertEqual(self.request("DELETE", f"{self.base}/{s['id']}", ctype=None)[0], 415)
        self.assertTrue(self.request("GET", self.base)[1]["sessions"][0]["running"])

    def test_unknown_sessions_and_roles(self):
        self.assertEqual(self.request("GET", f"{self.base}/nope/socket")[0], 404)
        self.assertEqual(self.request("POST", self.base, {"role": "nope"})[0], 400)

    def test_serves_the_vendored_terminal(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/vendor/xterm.js")
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn(b"Terminal", r.read())
        conn.close()


if __name__ == "__main__":
    unittest.main()
