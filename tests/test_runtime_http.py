"""Tests for the HTTP runtime runner (cadillac/runtime/http_runner.py).

LLM-generation tests use mocked `chat()`. The runner is tested against
real synthetic Flask apps booted on dynamic ports — one well-behaved, one
deliberately buggy (logout returns 200 but token still works).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from cadillac.languages import python_language
from cadillac.runtime.http_runner import (
    Flow,
    FlowStep,
    _check_subset,
    _extract_capture,
    _substitute,
    _substitute_body,
    generate_flows,
    run_flow,
)
from cadillac.spec import Spec, Story


def _flask_available() -> bool:
    try:
        import flask  # noqa: F401
        return True
    except ImportError:
        return False


class _Cfg:
    api_url = "http://localhost:8000/v1"
    model = "stub"
    context_window = 65536
    max_context_tokens = 40000
    stream = False
    enable_thinking = False
    api_key = None
    rate_limit = 0.0


# ── Pure-function tests ──────────────────────────────────────────────────────


class TestSubstitute(unittest.TestCase):
    def test_substitutes_known(self):
        self.assertEqual(_substitute("/x/{id}", {"id": 7}), "/x/7")

    def test_leaves_unknown(self):
        self.assertEqual(_substitute("/x/{missing}", {}), "/x/{missing}")

    def test_substitutes_body_strings(self):
        out = _substitute_body({"token": "Bearer {t}", "n": 5}, {"t": "abc"})
        self.assertEqual(out, {"token": "Bearer abc", "n": 5})


class TestExtractCapture(unittest.TestCase):
    def test_top_level(self):
        self.assertEqual(_extract_capture({"token": "abc"}, "$.token"), "abc")

    def test_nested(self):
        self.assertEqual(
            _extract_capture({"user": {"id": 42}}, "$.user.id"),
            42,
        )

    def test_missing_returns_none(self):
        self.assertIsNone(_extract_capture({"x": 1}, "$.missing"))


class TestCheckSubset(unittest.TestCase):
    def test_exact_match(self):
        ok, _ = _check_subset({"a": 1}, {"a": 1})
        self.assertTrue(ok)

    def test_exact_mismatch(self):
        ok, why = _check_subset({"a": 1}, {"a": 2})
        self.assertFalse(ok)
        self.assertIn("'a'", why)

    def test_gte_operator(self):
        ok, _ = _check_subset({"streak": 3}, {"streak": ">=1"})
        self.assertTrue(ok)
        ok, why = _check_subset({"streak": 0}, {"streak": ">=1"})
        self.assertFalse(ok)
        self.assertIn("streak", why)

    def test_not_null(self):
        ok, _ = _check_subset({"last": "2026-01-01"}, {"last": "!=null"})
        self.assertTrue(ok)
        ok, _ = _check_subset({"last": None}, {"last": "!=null"})
        self.assertFalse(ok)

    def test_missing_field(self):
        ok, why = _check_subset({"a": 1}, {"b": 1})
        self.assertFalse(ok)
        self.assertIn("missing", why)


# ── Flow-parsing via mocked chat ─────────────────────────────────────────────


class TestGenerateFlowsParse(unittest.TestCase):
    def test_parses_clean_json(self):
        payload = json.dumps({"flows": [
            {"story_id": "S01", "title": "signup + token",
             "priority": "must",
             "steps": [
                 {"method": "POST", "path": "/signup",
                  "body": {"email": "a@b"}, "expect_status": 201,
                  "capture": {"token": "$.token"}},
             ]},
        ]})
        with patch("cadillac.engine.chat",
                    return_value={"role": "assistant",
                                   "content": payload, "tool_calls": []}):
            with tempfile.TemporaryDirectory() as ws:
                spec = Spec(task="t", stories=[
                    Story(id="S01", title="signup", acceptance=("a",)),
                ])
                flows = generate_flows(spec, None, ws, python_language(), _Cfg(),
                                        emit=lambda *a, **k: None)
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0].steps[0].method, "POST")

    def test_garbage_returns_empty(self):
        with patch("cadillac.engine.chat",
                    return_value={"role": "assistant",
                                   "content": "not json", "tool_calls": []}):
            with tempfile.TemporaryDirectory() as ws:
                spec = Spec(task="t", stories=[
                    Story(id="S01", title="x", acceptance=("a",)),
                ])
                flows = generate_flows(spec, None, ws, python_language(), _Cfg(),
                                        emit=lambda *a, **k: None)
        self.assertEqual(flows, [])


# ── Runner against real synthetic Flask apps ─────────────────────────────────


_FLASK_GOOD = '''\
import os, secrets
from flask import Flask, jsonify, request

app = Flask(__name__)
_tokens = set()
_users = {}
_habits = {}
_next_habit_id = [1]

@app.post("/signup")
def signup():
    body = request.get_json(force=True) or {}
    email = body.get("email")
    if email in _users:
        return jsonify(error="duplicate"), 409
    token = secrets.token_hex(8)
    _users[email] = token
    _tokens.add(token)
    return jsonify(token=token), 201

def _auth():
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "):
        return None
    tok = hdr[7:]
    return tok if tok in _tokens else None

@app.post("/habits/")
def create_habit():
    if not _auth():
        return jsonify(error="unauthorized"), 401
    body = request.get_json(force=True) or {}
    hid = _next_habit_id[0]; _next_habit_id[0] += 1
    _habits[hid] = {"id": hid, "name": body.get("name"), "streak": 0}
    return jsonify(_habits[hid]), 201

@app.post("/habits/<int:hid>/complete")
def complete(hid):
    if not _auth(): return jsonify(error="unauthorized"), 401
    h = _habits.get(hid)
    if not h: return jsonify(error="not found"), 404
    h["streak"] += 1
    h["last_completed"] = "2026-05-18"
    return jsonify(h), 200

@app.post("/auth/logout")
def logout():
    tok = _auth()
    if not tok: return jsonify(error="unauthorized"), 401
    _tokens.discard(tok)   # invalidate
    return jsonify(message="logged out"), 200

@app.get("/habits/")
def list_habits():
    if not _auth(): return jsonify(error="unauthorized"), 401
    return jsonify(list(_habits.values())), 200

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
'''


_FLASK_BAD_LOGOUT = '''\
# Same as _FLASK_GOOD but logout doesn't invalidate the token (the real bug
# we observed in today's habit-tracker build).
import os, secrets
from flask import Flask, jsonify, request

app = Flask(__name__)
_tokens = set()

@app.post("/signup")
def signup():
    body = request.get_json(force=True) or {}
    token = secrets.token_hex(8)
    _tokens.add(token)
    return jsonify(token=token), 201

def _auth():
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "): return None
    tok = hdr[7:]
    return tok if tok in _tokens else None

@app.post("/auth/logout")
def logout():
    if not _auth(): return jsonify(error="unauthorized"), 401
    # BUG: doesn't invalidate the token
    return jsonify(message="logged out"), 200

@app.get("/habits/")
def list_habits():
    if not _auth(): return jsonify(error="unauthorized"), 401
    return jsonify([]), 200

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
'''


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _boot(workspace: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, "main.py"], cwd=workspace, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return proc
        except OSError:
            time.sleep(0.2)
        if proc.poll() is not None:
            break
    raise RuntimeError("server failed to bind")


def _kill(proc: subprocess.Popen) -> None:
    import signal
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            try: proc.wait(timeout=2)
            except Exception: pass
    except (ProcessLookupError, PermissionError, OSError):
        pass
    if proc.stdout:
        try: proc.stdout.close()
        except Exception: pass


@unittest.skipUnless(_flask_available(), "Flask not installed")
class TestRunFlowAgainstRealApp(unittest.TestCase):
    def test_happy_path_passes(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "main.py"), "w") as f:
                f.write(_FLASK_GOOD)
            port = _free_port()
            proc = _boot(ws, port)
            try:
                flow = Flow(story_id="S01", title="full happy path",
                             priority="must", steps=(
                    FlowStep(method="POST", path="/signup",
                              body={"email": "flow1@test.com"},
                              expect_status=201,
                              capture={"token": "$.token"}),
                    FlowStep(method="POST", path="/habits/",
                              body={"name": "Run"},
                              auth="bearer:{token}",
                              expect_status=201,
                              capture={"habit_id": "$.id"}),
                    FlowStep(method="POST",
                              path="/habits/{habit_id}/complete",
                              auth="bearer:{token}",
                              expect_status=200,
                              expect_response={"streak": ">=1",
                                                "last_completed": "!=null"}),
                ))
                fail = run_flow(flow, port)
                self.assertIsNone(fail, msg=str(fail))
            finally:
                _kill(proc)

    def test_broken_logout_caught(self):
        """The exact bug from today's habit-tracker run: logout returns 200
        but the token still works on subsequent requests."""
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "main.py"), "w") as f:
                f.write(_FLASK_BAD_LOGOUT)
            port = _free_port()
            proc = _boot(ws, port)
            try:
                flow = Flow(story_id="S03", title="logout invalidates token",
                             priority="must", steps=(
                    FlowStep(method="POST", path="/signup",
                              body={"email": "flow2@test.com"},
                              expect_status=201,
                              capture={"token": "$.token"}),
                    FlowStep(method="POST", path="/auth/logout",
                              auth="bearer:{token}",
                              expect_status=200),
                    # The assertion: post-logout request MUST 401.
                    FlowStep(method="GET", path="/habits/",
                              auth="bearer:{token}",
                              expect_status=401),
                ))
                fail = run_flow(flow, port)
                self.assertIsNotNone(fail,
                    "broken logout should be caught by the flow")
                self.assertEqual(fail.failure_kind, "status_mismatch")
                self.assertIn("got 200", fail.detail)
            finally:
                _kill(proc)


if __name__ == "__main__":
    unittest.main()
