"""Tests for kanvas_agents.py, the fork's terminal sessions and agents. Run with: python -m unittest discover -s tests -v

They need a POSIX pty, so they skip on Windows; run them in Docker (see docker-compose.yml). The agents'
tests run stand-ins for `claude` and `gh` against real git repositories, so they need git but no accounts.
"""

import atexit
import base64
import contextlib
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

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


def wait_until(check, timeout=20, what="the condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def sh(script):
    return lambda: ["sh", "-c", script]


@needs_pty
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.board = os.path.join(self.dir, "kanvas.json")

    def start(self, script, **kw):
        sessions = agents.Sessions(sh(script))
        self.addCleanup(sessions.stop_all)
        return sessions, sessions.start(self.board, "TR-01", "shell", **kw)

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
        wait_until(lambda: not s.running, what="the program to end")
        c = FakeClient()
        s.attach(c)
        self.assertIn(b"bye", c.data)
        self.assertEqual(c.texts, [{"t": "exit", "code": 0}])

    def test_stop_ends_what_the_program_started(self):
        marker = os.path.join(self.dir, "child.pid")
        sessions, s = self.start(f"sleep 60 & echo $! > {marker}; wait")
        def written():
            with contextlib.suppress(FileNotFoundError), open(marker) as f:
                return f.read().strip()
        wait_until(written, what="the child")
        child = int(written())
        sessions.remove(s.id)
        wait_until(lambda: not s.running, what="the program to end")
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
        sessions = agents.Sessions(lambda: ["no-such-program-kanvas"])
        self.addCleanup(sessions.stop_all)
        s = sessions.start(self.board, None, "shell")
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
        self.server.sessions.shell = sh('echo "ready"; while read line; do echo "said $line"; done')
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

    def test_start_attach_type_stop_and_remove(self):
        code, s = self.request("POST", self.base, {"role": "shell", "task": "TR-01"})
        self.assertEqual(code, 200, s)
        self.assertTrue(s["running"])
        code, listed = self.request("GET", self.base)
        self.assertEqual([x["id"] for x in listed["sessions"]], [s["id"]])
        self.assertEqual(listed["config"], {"merge": "manual", "auto_review": True})
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
        _, s = self.request("POST", self.base, {"role": "shell"})
        for origin in ("http://evil.example", False):
            ws = WsClient(self.port, f"{self.base}/{s['id']}/socket", origin=origin)
            self.addCleanup(ws.close)
            self.assertEqual(ws.status, 403, origin)

    def test_behind_tailscale_serve(self):
        # On the VPS, Kanvas listens on 0.0.0.0 (any Host) and `tailscale serve` passes on the tailnet name over HTTPS.
        self.server.hosts = None
        _, s = self.request("POST", self.base, {"role": "shell"})
        name = "kanvas.tail1234.ts.net"
        ok = WsClient(self.port, f"{self.base}/{s['id']}/socket", host=name, origin=f"https://{name}")
        self.addCleanup(ok.close)
        self.assertEqual(ok.status, 101)
        ok.read_until(lambda c: b"ready" in c.data)
        bad = WsClient(self.port, f"{self.base}/{s['id']}/socket", host=name, origin="https://evil.example")
        self.addCleanup(bad.close)
        self.assertEqual(bad.status, 403)

    def test_starting_and_stopping_need_json(self):
        # A form post from another site cannot send application/json without a CORS preflight.
        self.assertEqual(self.request("POST", self.base, {"role": "shell"}, ctype="text/plain")[0], 415)
        _, s = self.request("POST", self.base, {"role": "shell"})
        self.assertEqual(self.request("POST", f"{self.base}/{s['id']}/stop", {}, ctype="text/plain")[0], 415)
        self.assertEqual(self.request("DELETE", f"{self.base}/{s['id']}", ctype=None)[0], 415)
        self.assertTrue(self.request("GET", self.base)[1]["sessions"][0]["running"])

    def test_unknown_sessions_roles_and_tasks(self):
        self.assertEqual(self.request("GET", f"{self.base}/nope/socket")[0], 404)
        self.assertEqual(self.request("POST", self.base, {"role": "nope"})[0], 400)
        code, err = self.request("POST", self.base, {"role": "validator", "task": "XX-99"})
        self.assertEqual(code, 400)
        self.assertIn("XX-99", err["error"])

    def test_serves_the_vendored_terminal(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/vendor/xterm.js")
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn(b"Terminal", r.read())
        conn.close()


@needs_pty
class PromptTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.board = os.path.join(self.dir, "kanvas.json")

    def test_front_matter_and_template(self):
        meta, body = agents.parse_prompt("---\ntitle: Run\nallowed_tools: Bash(git push:*), Edit\n# a comment: ignored\n---\nDo {task.id}.\n")
        self.assertEqual(meta, {"title": "Run", "allowed_tools": "Bash(git push:*), Edit"})
        self.assertEqual(body, "Do {task.id}.\n")
        self.assertEqual(agents.parse_prompt("No front matter")[1], "No front matter\n")

    def test_tool_lists_keep_commas_inside_patterns(self):
        self.assertEqual(agents.tool_list("Read, Bash(a,b:*), Edit"), ["Read", "Bash(a,b:*)", "Edit"])
        self.assertEqual(agents.tool_list(""), [])

    def test_the_prompt_comes_before_options_that_take_several_values(self):
        argv = agents.agent_argv({"command": "claude --verbose", "model": "opus", "permission_mode": "acceptEdits",
                                  "allowed_tools": "Bash, Edit", "disallowed_tools": "Write"}, "Hi")
        self.assertEqual(argv, ["claude", "--verbose", "Hi", "--model", "opus", "--permission-mode", "acceptEdits",
                                "--allowedTools", "Bash", "Edit", "--disallowedTools", "Write"])

    def test_render_fills_known_names_only(self):
        self.assertEqual(agents.render_prompt("{task.id} on {branch}, {other} {x}", {"task.id": "TS-01", "branch": "b"}),
                         "TS-01 on b, {other} {x}")

    def test_defaults_are_copied_once_and_never_overwritten(self):
        meta, body = agents.load_prompt(self.board, "reviewer")
        self.assertEqual(meta["title"], "Review")
        self.assertIn("kanvas verdict {task.id} approve", body)
        path = os.path.join(self.dir, ".kanvas", "agents", "reviewer.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("---\ntitle: Mine\n---\nMy own reviewer\n")
        self.assertEqual(agents.load_prompt(self.board, "reviewer"), ({"title": "Mine"}, "My own reviewer\n"))

    def test_every_role_ships_a_prompt(self):
        for role in agents.AGENT_ROLES:
            meta, body = agents.load_prompt(self.board, role)
            self.assertIn("{task.id}", body, role)
            self.assertEqual(meta["command"], "claude", role)

    def test_config_is_created_with_defaults(self):
        self.assertEqual(agents.load_config(self.board), {"merge": "manual", "auto_review": True})
        with open(os.path.join(self.dir, ".kanvas", "config.json"), "w") as f:
            f.write('{"merge": "auto", "base": "dev"}')
        self.assertEqual(agents.load_config(self.board), {"merge": "auto", "auto_review": True, "base": "dev"})


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@needs_pty
class AgentFlowTests(unittest.TestCase):
    """The three agents on a real git project with a local "origin", with stand-ins for claude and gh."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "T",
               "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull}
        # Stand-in for gh: `pr merge` is logged, so the tests see what would have been merged.
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.gh_log = os.path.join(self.dir, "gh.log")
        self.script("bin/gh", f'echo "$@" >> {self.gh_log}\n[ "$GH_FAIL" = 1 ] && {{ echo "merge refused" >&2; exit 1; }}\nexit 0')
        env["PATH"] = self.bin + os.pathsep + os.environ["PATH"]
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.origin = os.path.join(self.dir, "origin.git")
        git("init", "-q", "--bare", "-b", "main", self.origin, cwd=self.dir)
        self.proj = os.path.join(self.dir, "proj")
        git("clone", "-q", self.origin, self.proj, cwd=self.dir)
        git("checkout", "-q", "-b", "main", cwd=self.proj)
        self.board = os.path.join(self.proj, "kanvas.json")
        board = kanvas.new_board("Flow")
        board["tasks"] = [
            {"id": "TS-01", "title": "Add a feature", "state": "proposed", "desc": "Rough idea", "deps": [], "notes": []},
            {"id": "TS-02", "title": "Later", "state": "todo", "desc": "", "deps": ["TS-01"], "notes": []},
        ]
        with open(self.board, "w", encoding="utf-8", newline="\n") as f:
            f.write(kanvas.dumps(board))
        git("add", "kanvas.json", cwd=self.proj)
        git("commit", "-q", "-m", "board", cwd=self.proj)
        git("push", "-q", "-u", "origin", "main", cwd=self.proj)
        git("remote", "set-head", "origin", "main", cwd=self.proj)
        self.sessions = agents.Sessions()
        self.addCleanup(self.sessions.stop_all)

    def script(self, rel, body):
        path = os.path.join(self.dir, rel)
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def agent(self, role, body, front=""):
        """Makes the project's prompt for `role` run a shell script instead of claude. The rendered prompt is $1."""
        cmd = self.script(f"{role}.sh", "set -e\n" + body)
        prompts = os.path.join(self.proj, ".kanvas", "agents")
        os.makedirs(prompts, exist_ok=True)
        with open(os.path.join(prompts, f"{role}.md"), "w", encoding="utf-8") as f:
            f.write(f"---\ntitle: {role.title()}\ncommand: {cmd}\nallowed_tools: Bash, Edit\n{front}---\n"
                    "PROMPT for {task.id}: branch={branch} base={base} pr={pr} merge={merge}\n{task.notes}\n")

    def task(self, tid):
        return kanvas.find_task(kanvas.load_board(self.board), tid)

    def set_task(self, tid, **fields):
        def op(b):
            kanvas.find_task(b, tid).update(fields)
        kanvas.mutate(self.board, op)

    def run_agent(self, tid, role):
        s = self.sessions.start(self.board, tid, role)
        c = FakeClient()
        s.attach(c)
        wait_until(lambda: not s.running, what=f"the {role} to end")
        c.wait_for(lambda c: c.texts)
        return s, bytes(c.data).decode("utf-8", "replace")

    def test_the_validator_agrees_the_description_and_approves(self):
        self.agent("validator", 'echo "$1" > prompt.txt\nkanvas validate TS-01 --desc "Clear goal" -m "agreed with the human"')
        s, out = self.run_agent("TS-01", "validator")
        self.assertEqual(s.exit_code, 0, out)
        t = self.task("TS-01")
        self.assertEqual((t["state"], t["desc"]), ("todo", "Clear goal"))
        self.assertIn("validated", t["agent"])
        self.assertEqual((t["notes"][-1]["by"], t["notes"][-1]["text"]), ("validator", "Validated: agreed with the human"))
        with open(os.path.join(self.proj, "prompt.txt")) as f:
            self.assertIn("PROMPT for TS-01", f.read())

    def test_only_the_matching_agent_may_validate_record_or_decide(self):
        cases = [["validate", "TS-01", "--desc", "x"], ["pr", "TS-01", "https://x/pull/1"], ["verdict", "TS-01", "approve", "-m", "ok"]]
        for argv in cases:
            for env in ({}, {"KANVAS_ROLE": "executor"}):
                with mock.patch.dict(os.environ, dict(env, KANVAS_BOARD=self.board)), contextlib.redirect_stderr(io.StringIO()) as err:
                    role = {"validate": "validator", "pr": "executor", "verdict": "reviewer"}[argv[0]]
                    if env.get("KANVAS_ROLE") == role:
                        continue
                    self.assertEqual(kanvas.main(argv + ["--as", role]), 1, (argv, env))
                self.assertIn(f"Only the {role} agent", err.getvalue())
        self.assertEqual(self.task("TS-01")["state"], "proposed")

    def test_the_executor_opens_a_pull_request_and_the_reviewer_merges_it(self):
        os.makedirs(os.path.join(self.proj, ".kanvas"), exist_ok=True)
        with open(os.path.join(self.proj, ".kanvas", "config.json"), "w") as f:
            f.write('{"merge": "auto", "auto_review": true}')
        self.set_task("TS-01", state="todo")
        self.agent("executor", textwrap.dedent("""\
            echo "$1" > ../../../executor-prompt.txt
            kanvas start TS-01 -m "plan"
            echo feature > feature.txt
            git add feature.txt && git commit -q -m "Add the feature"
            git push -q -u origin "$KANVAS_BRANCH"
            kanvas pr TS-01 https://github.com/o/r/pull/7
            kanvas finish TS-01 -m "Added feature.txt"
            """))
        self.agent("reviewer", textwrap.dedent("""\
            echo "$KANVAS_ROLE $KANVAS_PR $(git rev-parse --abbrev-ref HEAD) $(cat feature.txt)" > ../../../review.txt
            kanvas verdict TS-01 approve -m "Looks right"
            """))
        s, out = self.run_agent("TS-01", "executor")
        self.assertEqual(s.exit_code, 0, out)
        with open(os.path.join(self.proj, "executor-prompt.txt")) as f:
            self.assertIn("PROMPT for TS-01: branch=kanvas/ts-01 base=main pr=none merge=merged automatically", f.read())
        self.assertIn("kanvas/ts-01", git("ls-remote", "--heads", self.origin, cwd=self.dir))
        # The reviewer starts by itself when the executor ends with the task in review.
        wait_until(lambda: any(x.role == "reviewer" and not x.running for x in self.sessions.list(self.board)), what="the reviewer")
        with open(os.path.join(self.proj, "review.txt")) as f:
            self.assertEqual(f.read().strip(), "reviewer https://github.com/o/r/pull/7 kanvas/ts-01 feature")
        t = self.task("TS-01")
        self.assertEqual(t["state"], "done")
        self.assertEqual(t["agent"]["pr"], "https://github.com/o/r/pull/7")
        self.assertEqual([n["text"] for n in t["notes"][-2:]], ["Review passed: Looks right", "Merged https://github.com/o/r/pull/7"])
        with open(self.gh_log) as f:
            self.assertEqual(f.read().strip(), "pr merge https://github.com/o/r/pull/7 --squash")
        self.assertFalse(os.path.exists(os.path.join(self.proj, ".kanvas", "worktrees", "TS-01")))
        # The worktrees stay out of git status.
        self.assertNotIn("worktrees", git("status", "--porcelain", "--untracked-files=all", cwd=self.proj))

    def test_manual_merge_leaves_the_pull_request_to_the_human(self):
        self.set_task("TS-01", state="review", agent={"pr": "https://github.com/o/r/pull/8"})
        self.agent("reviewer", 'kanvas verdict TS-01 approve -m "Fine"')
        s, out = self.run_agent("TS-01", "reviewer")
        self.assertEqual(s.exit_code, 0, out)
        self.assertEqual(self.task("TS-01")["state"], "done")
        self.assertFalse(os.path.exists(self.gh_log))

    def test_a_failed_merge_is_noted(self):
        self.set_task("TS-01", state="review", agent={"pr": "https://github.com/o/r/pull/9", "merge": "auto"})
        self.agent("reviewer", 'GH_FAIL=1 kanvas verdict TS-01 approve -m "Fine" || echo "verdict exited $?"')
        s, out = self.run_agent("TS-01", "reviewer")
        self.assertIn("verdict exited 1", out)
        t = self.task("TS-01")
        self.assertEqual(t["state"], "done")
        self.assertIn("merging the pull request failed", t["notes"][-1]["text"])
        self.assertIn("merge refused", t["notes"][-1]["text"])

    def test_changes_send_the_task_back_to_be_validated(self):
        self.set_task("TS-01", state="review", agent={"pr": "https://github.com/o/r/pull/7", "branch": "kanvas/ts-01"})
        self.agent("reviewer", 'kanvas verdict TS-01 changes -m "Handle the empty case"')
        self.run_agent("TS-01", "reviewer")
        t = self.task("TS-01")
        self.assertEqual(t["state"], "todo")
        self.assertTrue(t["agent"]["needs_validation"])
        self.assertEqual(t["notes"][-1]["text"], "Changes requested: Handle the empty case")
        with self.assertRaisesRegex(agents.SessionError, "validate it again"):
            self.sessions.start(self.board, "TS-01", "executor")
        # Validating again clears the flag; the feedback is in the notes the validator and executor read.
        self.agent("validator", 'echo "$1" > ../v.txt; kanvas validate TS-01 -m "now covers the empty case"')
        self.run_agent("TS-01", "validator")
        self.assertNotIn("needs_validation", self.task("TS-01")["agent"])
        with open(os.path.join(self.dir, "v.txt")) as f:
            self.assertIn("Changes requested: Handle the empty case", f.read())

    def test_a_second_run_continues_the_same_branch(self):
        self.set_task("TS-01", state="todo")
        self.agent("executor", 'git commit -q --allow-empty -m first && git push -q -u origin "$KANVAS_BRANCH"\nkanvas start TS-01\nkanvas pause TS-01 -m "stopping"')
        self.run_agent("TS-01", "executor")
        # Only the pushed branch is left: no worktree, no local branch.
        shutil.rmtree(os.path.join(self.proj, ".kanvas", "worktrees"))
        git("worktree", "prune", cwd=self.proj)
        git("branch", "-D", "kanvas/ts-01", cwd=self.proj)
        self.agent("executor", 'git log --format=%s -1 > ../../../log.txt')
        self.run_agent("TS-01", "executor")
        with open(os.path.join(self.proj, "log.txt")) as f:
            self.assertEqual(f.read().strip(), "first")

    def test_what_each_agent_may_start_on(self):
        with self.assertRaisesRegex(agents.SessionError, "only tasks ready to start can run"):
            self.sessions.start(self.board, "TS-01", "executor")
        self.set_task("TS-01", state="todo")
        with self.assertRaisesRegex(agents.SessionError, "only tasks ready to start can run"):
            self.sessions.start(self.board, "TS-02", "executor")  # blocked by TS-01
        with self.assertRaisesRegex(agents.SessionError, "only tasks in review"):
            self.sessions.start(self.board, "TS-01", "reviewer")
        self.set_task("TS-01", state="review")
        with self.assertRaisesRegex(agents.SessionError, "no pull request"):
            self.sessions.start(self.board, "TS-01", "reviewer")
        with self.assertRaisesRegex(agents.SessionError, "only proposals and tasks to do"):
            self.sessions.start(self.board, "TS-01", "validator")
        with self.assertRaisesRegex(agents.SessionError, "works on a task"):
            self.sessions.start(self.board, None, "validator")

    def test_one_agent_at_a_time_per_task(self):
        self.agent("validator", "sleep 5")
        self.sessions.start(self.board, "TS-01", "validator")
        with self.assertRaisesRegex(agents.SessionError, "already has its validator agent running"):
            self.sessions.start(self.board, "TS-01", "validator")

    def test_running_needs_a_git_repository(self):
        plain = os.path.join(self.dir, "plain")
        os.makedirs(plain)
        board = os.path.join(plain, "kanvas.json")
        b = kanvas.new_board("Plain")
        b["tasks"] = [{"id": "PL-01", "title": "x", "state": "todo", "desc": "", "deps": [], "notes": []}]
        with open(board, "w", encoding="utf-8", newline="\n") as f:
            f.write(kanvas.dumps(b))
        with self.assertRaisesRegex(agents.SessionError, "not a git repository"):
            self.sessions.start(board, "PL-01", "executor")


if __name__ == "__main__":
    unittest.main()
