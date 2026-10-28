"""Kanvas agents (jaimarad/Kanvas fork): terminal sessions and the agents that run in them.

A session is a program, such as a shell or an agent, running in a pseudo-terminal on the computer that runs
Kanvas. Its output is kept, so a browser that attaches later, or again after a reload, sees the screen as it
was; any number of browsers can attach at once and type into it. Sessions outlive the browser: closing the
tab leaves them running until the program ends or someone stops it.

Three agents work a task through the board, each with its own prompt in the project's .kanvas/agents/:

- the validator checks with the human that the task is doable and agrees its description (proposed -> todo);
- the executor does it on branch kanvas/<id> in a worktree of its own and opens a pull request (todo -> review);
- the reviewer reviews the pull request and gives a verdict: done, merging it in Auto-merge mode, or back to
  To do, where it must be validated again before it runs.

The executor's session starts the reviewer when it ends with the task in review. Agents report through the
CLI, whose `validate`, `pr` and `verdict` commands this module adds and only the matching agent may use.
What the fork keeps per task lives in the task's "agent" object: needs_validation, validated, branch, base,
pr and merge. Project settings live in .kanvas/config.json: merge ("manual" or "auto") and auto_review.

The browser talks to a session over a WebSocket, implemented here on top of http.server so Kanvas still
needs only the standard library. Binary frames carry the terminal's bytes both ways; text frames carry
JSON control messages: {"t": "r", "c": cols, "r": rows} from the browser to resize, and
{"t": "exit", "code": n} to the browser when the program ends.

Linux-first: this needs a POSIX pty, so kanvas.py loads it only where os.name is "posix", and sets K to itself.
"""

import base64
import hashlib
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

import fcntl
import termios

K = None  # the kanvas module, set by kanvas.py when it loads this one

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(HERE, "vendor")
DEFAULT_PROMPTS = os.path.join(HERE, "agents")
VENDOR_FILES = {
    "xterm.js": ("xterm/xterm.js", "text/javascript; charset=utf-8"),
    "xterm.css": ("xterm/xterm.css", "text/css; charset=utf-8"),
    "addon-fit.js": ("xterm/addon-fit.js", "text/javascript; charset=utf-8"),
}

AGENT_ROLES = ("validator", "executor", "reviewer")
DEFAULT_CONFIG = {"merge": "manual", "auto_review": True}
MERGE_MODES = ("manual", "auto")

SCROLLBACK = 512 * 1024  # bytes of output kept per session, replayed to a browser that attaches
MAX_RUNNING = 16
MAX_FRAME = 1024 * 1024
STOP_GRACE = 3.0  # seconds between SIGHUP/SIGTERM and SIGKILL
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


class SessionError(Exception):
    pass


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class Session:
    def __init__(self, board, task, role, argv, cwd, env=None, title=None, on_exit=None):
        self.id = secrets.token_hex(4)
        self.board = board
        self.task = task
        self.role = role
        self.title = title or role.title()
        self.argv = list(argv)
        self.cwd = cwd
        self.on_exit = on_exit
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
        if self.on_exit:
            try:
                self.on_exit(self)
            except Exception as e:  # a failed follow-up must not take the server down
                print(f"Kanvas: after session {self.id} ({self.role} of {self.task}): {e}", file=sys.stderr, flush=True)

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
        return {"id": self.id, "task": self.task, "role": self.role, "title": self.title,
                "running": self.running, "exit": self.exit_code, "started": _iso(self.started),
                "ended": _iso(self.ended) if self.ended else None}


def _set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _shell():
    return [os.environ.get("SHELL") or "/bin/bash", "-l"]


class Sessions:
    """Every session this Kanvas runs, for all its boards."""

    def __init__(self, shell=None):
        self.shell = shell or _shell
        self.items = {}  # id -> Session, oldest first
        self.lock = threading.Lock()

    def _add(self, session, cols, rows):
        with self.lock:
            if sum(s.running for s in self.items.values()) >= MAX_RUNNING:
                raise SessionError(f"{MAX_RUNNING} sessions are already running; stop one first")
            session.start(cols, rows)
            self.items[session.id] = session
        return session

    def start(self, board, task=None, role="shell", cols=80, rows=24):
        if role == "shell":
            s = Session(board, task, "shell", self.shell(), os.path.dirname(board), title="Terminal")
            return self._add(s, cols, rows)
        if role in AGENT_ROLES:
            if not task:
                raise SessionError("An agent works on a task; say which")
            return self.start_agent(board, task, role, cols, rows)
        raise SessionError(f"Unknown role {role!r}; expected shell, {', '.join(AGENT_ROLES)}")

    def start_agent(self, board, task_id, role, cols=120, rows=32):
        with self.lock:
            busy = [s for s in self.items.values() if s.running and s.role != "shell" and s.board == board
                    and (s.task or "").upper() == task_id.upper()]
        if busy:
            raise SessionError(f"{task_id} already has its {busy[0].title.lower()} agent running")
        b, t = _load_task(board, task_id)
        vs = K.view_state(t, K.task_map(b))
        ag = t.get("agent") or {}
        cfg = load_config(board)
        project = os.path.dirname(board)
        env = {}
        if role == "validator":
            if t["state"] not in ("proposed", "todo"):
                raise SessionError(f"{t['id']} is {vs}; only proposals and tasks to do are validated")
            cwd = project
        elif role == "executor":
            if vs != "todo":
                raise SessionError(f"{t['id']} is {vs}; only tasks ready to start can run")
            if ag.get("needs_validation"):
                raise SessionError(f"The reviewer sent {t['id']} back; validate it again before it runs")
            wt = prepare_worktree(board, t, cfg)
            cwd = wt["cwd"]
            ag = _record_branch(board, t["id"], wt)
        elif role == "reviewer":
            if t["state"] != "review":
                raise SessionError(f"{t['id']} is {vs}; only tasks in review are reviewed")
            if not ag.get("pr"):
                raise SessionError(f"{t['id']} has no pull request; the executor records it with `kanvas pr`")
            wt = prepare_worktree(board, t, cfg)
            cwd = wt["cwd"]
            ag = _record_branch(board, t["id"], wt)
        meta, body = load_prompt(board, role)
        values = prompt_values(b, t, ag, cfg)
        argv = agent_argv(meta, render_prompt(body, values))
        env.update(KANVAS_ROLE=role, KANVAS_AGENT=role, KANVAS_BRANCH=ag.get("branch") or "", KANVAS_PR=ag.get("pr") or "",
                   PATH=kanvas_bin() + os.pathsep + os.environ.get("PATH", ""))
        on_exit = self._after_executor if role == "executor" else None
        s = Session(board, t["id"], role, argv, cwd, env, title=meta.get("title") or role.title(), on_exit=on_exit)
        return self._add(s, cols, rows)

    def _after_executor(self, session):
        """Hands a task the executor finished over to the reviewer, unless the project turned that off."""
        if not load_config(session.board).get("auto_review"):
            return
        _, t = _load_task(session.board, session.task)
        if t["state"] == "review" and (t.get("agent") or {}).get("pr"):
            self.start_agent(session.board, t["id"], "reviewer")

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


def _load_task(board, task_id):
    try:
        b = K.load_board(board)
        return b, K.find_task(b, task_id)
    except K.KanvasError as e:
        raise SessionError(str(e))


def _record_branch(board, task_id, wt):
    def op(b):
        t = K.find_task(b, task_id)
        ag = dict(t.get("agent") or {})
        ag["branch"], ag["base"] = wt["branch"], wt["base"]
        t["agent"] = ag
        return ag
    return K.mutate(board, op)


_BIN = None
_BIN_LOCK = threading.Lock()


def kanvas_bin():
    """A folder with a `kanvas` command that runs this Kanvas, for the PATH of agent sessions: agents then use
    the CLI with this fork's commands whatever else is installed."""
    global _BIN
    with _BIN_LOCK:
        if _BIN is None or not os.path.isdir(_BIN):
            _BIN = tempfile.mkdtemp(prefix="kanvas-bin-")
            path = os.path.join(_BIN, "kanvas")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(K.__file__))} \"$@\"\n")
            os.chmod(path, 0o755)
        return _BIN


# ---------------------------------------------------------------------------
# Project settings and prompts
# ---------------------------------------------------------------------------


def kanvas_dir(board):
    return os.path.join(os.path.dirname(board), ".kanvas")


def load_config(board):
    """The project's settings in .kanvas/config.json, created with the defaults the first time."""
    path = os.path.join(kanvas_dir(board), "config.json")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        except FileExistsError:
            pass
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise SessionError(f"Cannot read {path}: {e}")
    if not isinstance(cfg, dict):
        raise SessionError(f"{path} must hold a JSON object")
    out = dict(DEFAULT_CONFIG, **cfg)
    if out.get("merge") not in MERGE_MODES:
        out["merge"] = "manual"
    return out


def load_prompt(board, role):
    """(settings, template) of the role's prompt in .kanvas/agents/<role>.md, copied from Kanvas's own the first
    time. A project's copy is never overwritten, so it can be tuned."""
    path = os.path.join(kanvas_dir(board), "agents", f"{role}.md")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(os.path.join(DEFAULT_PROMPTS, f"{role}.md"), encoding="utf-8") as src, \
                    open(path, "x", encoding="utf-8", newline="\n") as dst:
                dst.write(src.read())
        except FileExistsError:
            pass
    try:
        with open(path, encoding="utf-8") as f:
            return parse_prompt(f.read())
    except OSError as e:
        raise SessionError(f"Cannot read the {role}'s prompt: {e}")


def parse_prompt(text):
    """Settings from a front matter of `key: value` lines between --- lines, and the prompt after it."""
    meta = {}
    m = re.match(r"---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        text = text[m.end():]
    return meta, text.strip() + "\n"


def tool_list(value):
    """Splits a comma-separated tool list, keeping the commas inside Bash(...) patterns."""
    out, depth, cur = [], 0, ""
    for ch in value or "":
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth <= 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return [x for x in out if x]


def agent_argv(meta, prompt):
    """The agent's command line: the prompt first, then the options, since options such as --allowedTools take
    several values and would swallow a prompt that came after them."""
    argv = shlex.split(meta.get("command") or "claude") + [prompt]
    if meta.get("model"):
        argv += ["--model", meta["model"]]
    if meta.get("permission_mode"):
        argv += ["--permission-mode", meta["permission_mode"]]
    if tool_list(meta.get("allowed_tools")):
        argv += ["--allowedTools", *tool_list(meta["allowed_tools"])]
    if tool_list(meta.get("disallowed_tools")):
        argv += ["--disallowedTools", *tool_list(meta["disallowed_tools"])]
    return argv


def prompt_values(board, t, ag, cfg):
    group = next((g["name"] for g in board["groups"] if g["id"] == t.get("group")), None)
    notes = "\n\n".join(f"- {n.get('by') or '?'} at {n.get('at') or '?'}:\n{_indent(n.get('text') or '')}" for n in t.get("notes", []))
    merge = ag.get("merge") or cfg["merge"]
    return {
        "task.id": t["id"], "task.title": t["title"], "task.state": t["state"],
        "task.group": group or "none", "task.desc": t.get("desc") or "(no description yet)",
        "task.notes": notes or "(none)", "branch": ag.get("branch") or "", "base": ag.get("base") or "",
        "pr": ag.get("pr") or "none",
        "merge": "merged automatically" if merge == "auto" else "left for the human to merge",
    }


def _indent(text):
    return "\n".join("  " + line for line in text.splitlines())


def render_prompt(template, values):
    """Fills {task.id} and the like; anything else in braces stays as written."""
    return re.sub(r"\{([a-z_.]+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template)


# ---------------------------------------------------------------------------
# Git: one worktree and branch per task
# ---------------------------------------------------------------------------


def git(args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise SessionError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r


def prepare_worktree(board, t, cfg):
    """The task's worktree at <repo>/.kanvas/worktrees/<ID> on branch kanvas/<id>, created from the base branch
    the first time and kept for the reviewer and for later runs, which continue the same branch and pull request."""
    project = os.path.dirname(board)
    r = git(["rev-parse", "--show-toplevel"], project, check=False)
    if r.returncode:
        raise SessionError("This project is not a git repository; agents work on branches and open pull requests")
    top = r.stdout.strip()
    sub = os.path.relpath(os.path.realpath(project), os.path.realpath(top))
    ag = t.get("agent") or {}
    branch = ag.get("branch") or f"kanvas/{t['id'].lower()}"
    wt = os.path.join(top, ".kanvas", "worktrees", t["id"])
    _exclude(top, "/.kanvas/worktrees/")
    has_origin = not git(["remote", "get-url", "origin"], top, check=False).returncode
    base = ag.get("base") or cfg.get("base") or _base_branch(top, has_origin)
    if not (os.path.isdir(wt) and not git(["rev-parse", "--git-dir"], wt, check=False).returncode):
        git(["worktree", "prune"], top, check=False)
        if has_origin:
            git(["fetch", "origin"], top, check=False)
        if _exists(top, f"refs/heads/{branch}"):
            git(["worktree", "add", wt, branch], top)
        elif has_origin and _exists(top, f"refs/remotes/origin/{branch}"):
            git(["worktree", "add", "--track", "-b", branch, wt, f"origin/{branch}"], top)
        else:
            start = f"origin/{base}" if has_origin and _exists(top, f"refs/remotes/origin/{base}") else base
            git(["worktree", "add", "--no-track", "-b", branch, wt, start], top)
    return {"top": top, "path": wt, "cwd": os.path.join(wt, sub) if sub != "." else wt, "branch": branch, "base": base}


def _exists(top, ref):
    return not git(["rev-parse", "--verify", "--quiet", ref], top, check=False).returncode


def _base_branch(top, has_origin):
    if has_origin:
        r = git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], top, check=False)
        if not r.returncode and r.stdout.strip().startswith("origin/"):
            return r.stdout.strip()[len("origin/"):]
    r = git(["rev-parse", "--abbrev-ref", "HEAD"], top, check=False)
    return r.stdout.strip() if not r.returncode and r.stdout.strip() != "HEAD" else "main"


def _exclude(top, pattern):
    """Keeps the worktrees out of `git status` through .git/info/exclude, which is not a file of the user's."""
    common = git(["rev-parse", "--git-common-dir"], top).stdout.strip()
    path = os.path.join(top if not os.path.isabs(common) else "", common, "info", "exclude")
    try:
        with open(path, encoding="utf-8") as f:
            if pattern in f.read().splitlines():
                return
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n# Kanvas: one worktree per task\n{pattern}\n")


def remove_worktree(board, ag):
    """Removes the task's worktree and local branch after its pull request is merged; best effort."""
    project = os.path.dirname(board)
    r = git(["rev-parse", "--show-toplevel"], project, check=False)
    if r.returncode or not ag.get("branch"):
        return
    top = r.stdout.strip()
    for wt in _worktrees_on(top, ag["branch"]):
        git(["worktree", "remove", "--force", wt], top, check=False)
    git(["branch", "-D", ag["branch"]], top, check=False)


def _worktrees_on(top, branch):
    out, path = [], None
    for line in git(["worktree", "list", "--porcelain"], top, check=False).stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path and os.path.realpath(path) != os.path.realpath(top):
            out.append(path)
    return out


# ---------------------------------------------------------------------------
# CLI commands for the agents: validate, pr, verdict
# ---------------------------------------------------------------------------


def add_commands(add, text_sources):
    p = add("validate", cmd_validate, "validator: agree a task's description with the human and approve it (proposed -> todo)")
    p.add_argument("id")
    p.add_argument("--desc", dest="new_desc", metavar="DESC", help="the agreed description")
    p.add_argument("-m", "--message", help="one line on what was agreed")
    text_sources(p, "description")
    p = add("pr", cmd_pr, "executor: record the task's pull request")
    p.add_argument("id")
    p.add_argument("url")
    p = add("verdict", cmd_verdict, "reviewer: approve the pull request (review -> done) or ask for changes (review -> todo)")
    p.add_argument("id")
    p.add_argument("decision", choices=("approve", "changes"))
    p.add_argument("-m", "--message", help="summary of the review, or what must change")
    text_sources(p, "summary")


def require_role(args, role):
    """Only the agent Kanvas starts for `role` may approve, record or decide in its name. It runs with
    KANVAS_ROLE set; other agents keep the upstream workflow."""
    if K.actor(args) != role or os.environ.get("KANVAS_ROLE") != role:
        raise K.KanvasError(f"Only the {role} agent that Kanvas starts from the board may do this")


def cmd_validate(args):
    require_role(args, "validator")
    path = K.board_path(args)
    desc = K.read_input(args.new_desc, args.stdin, args.file, "description")

    def op(board):
        t = K.find_task(board, args.id)
        if t["state"] == "proposed":
            t["state"] = "todo"
        elif t["state"] != "todo":
            raise K.KanvasError(f"{t['id']} is {K.view_state(t, K.task_map(board))}; only proposals and tasks to do are validated")
        if desc:
            t["desc"] = desc
        ag = dict(t.get("agent") or {})
        ag.pop("needs_validation", None)
        ag["validated"] = K.now_iso()
        t["agent"] = ag
        K.add_note(t, "Validated" + (f": {args.message}" if args.message else "."), K.actor(args))
        return t

    t = K.mutate(path, op)
    print(f"Validated {t['id']}; it is ready to run once its dependencies are done.")


def cmd_pr(args):
    require_role(args, "executor")
    path = K.board_path(args)
    if not re.match(r"https?://", args.url):
        raise K.KanvasError("Expected the pull request's URL")

    def op(board):
        t = K.find_task(board, args.id)
        if t["state"] not in ("doing", "review"):
            raise K.KanvasError(f"{t['id']} is {t['state']}; record its pull request while you work on it")
        ag = dict(t.get("agent") or {})
        ag["pr"] = args.url
        if os.environ.get("KANVAS_BRANCH"):
            ag.setdefault("branch", os.environ["KANVAS_BRANCH"])
        t["agent"] = ag
        return t

    t = K.mutate(path, op)
    print(f"Recorded the pull request of {t['id']}: {args.url}")


def cmd_verdict(args):
    require_role(args, "reviewer")
    path = K.board_path(args)
    summary = K.read_input(args.message, args.stdin, args.file, "summary")
    if not summary:
        raise K.KanvasError("verdict needs a summary of the review, or of what must change (-m or --stdin)")
    cfg = load_config(path)
    who = K.actor(args)

    def op(board):
        t = K.find_task(board, args.id)
        if t["state"] != "review":
            raise K.KanvasError(f"{t['id']} is {K.view_state(t, K.task_map(board))}; only tasks in review get a verdict")
        ag = dict(t.get("agent") or {})
        if args.decision == "approve":
            t["state"] = "done"
            K.add_note(t, f"Review passed: {summary}", who)
        else:
            t["state"] = "todo"
            ag["needs_validation"] = True
            K.add_note(t, f"Changes requested: {summary}", who)
        t["agent"] = ag
        return t, ag

    t, ag = K.mutate(path, op)
    if args.decision == "changes":
        print(f"Sent {t['id']} back (review -> todo). It must be validated again before it runs.")
        return
    merge = ag.get("merge") or cfg["merge"]
    if merge != "auto" or not ag.get("pr"):
        print(f"Approved {t['id']} (review -> done). " + ("Its pull request waits for the human to merge it." if ag.get("pr") else ""))
        return
    r = subprocess.run(["gh", "pr", "merge", ag["pr"], "--squash"], cwd=os.path.dirname(path), capture_output=True, text=True)
    if r.returncode:
        reason = (r.stderr or r.stdout).strip() or f"gh exited with {r.returncode}"
        _note(path, t["id"], f"Approved, but merging the pull request failed; merge it by hand: {reason}", who)
        print(f"Approved {t['id']} (review -> done), but merging failed: {reason}", file=sys.stderr)
        return 1
    _note(path, t["id"], f"Merged {ag['pr']}", who)
    remove_worktree(path, ag)
    print(f"Approved {t['id']} (review -> done) and merged {ag['pr']}.")


def _note(path, task_id, text, who):
    K.mutate(path, lambda b: K.add_note(K.find_task(b, task_id), text, who))


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

    GET lists the board's sessions with the project's settings, POST starts one (a shell, or an agent for a
    task), POST <id>/stop ends its program but keeps its output, DELETE <id> ends it and forgets it, and
    GET <id>/socket attaches over a WebSocket."""
    if method == "GET" and rest in ([], [""]):
        try:
            cfg = load_config(board)
        except (SessionError, OSError):
            cfg = dict(DEFAULT_CONFIG)
        body = {"sessions": [s.info() for s in sessions.list(board)], "config": cfg}
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
        except (OSError, K.KanvasError) as e:
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
