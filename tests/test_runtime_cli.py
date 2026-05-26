"""Tests for the CLI runtime runner (cadillac/runtime/cli_runner.py)."""

from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from cadillac.languages import python_language
from cadillac.runtime.cli_runner import (
    ScriptedRun,
    generate_runs,
    run_scripted,
)
from cadillac.spec import Spec, Story


class _Cfg:
    api_url = "http://localhost:8000/v1"
    model = "stub"
    context_window = 65536
    max_context_tokens = 40000
    stream = False
    enable_thinking = False
    api_key = None
    rate_limit = 0.0


def _mk(ws: str, rel: str, content: str) -> None:
    full = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(full) or ws, exist_ok=True)
    with open(full, "w") as f:
        f.write(textwrap.dedent(content))


class TestGenerateRunsParse(unittest.TestCase):
    def test_parses_clean_json(self):
        payload = json.dumps({"runs": [
            {"story_id": "S01", "title": "echo",
             "priority": "must",
             "argv": ["python3", "main.py", "hi"],
             "expect_stdout_contains": ["hi"]},
        ]})
        with patch("cadillac.engine.chat",
                    return_value={"role": "assistant",
                                   "content": payload, "tool_calls": []}):
            with tempfile.TemporaryDirectory() as ws:
                spec = Spec(task="t", stories=[
                    Story(id="S01", title="echo", acceptance=("prints",)),
                ])
                runs = generate_runs(spec, ws, python_language(), _Cfg(), emit=lambda *a, **k: None)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].argv, ("python3", "main.py", "hi"))

    def test_garbage_returns_empty(self):
        with patch("cadillac.engine.chat",
                    return_value={"role": "assistant",
                                   "content": "not json", "tool_calls": []}):
            with tempfile.TemporaryDirectory() as ws:
                spec = Spec(task="t", stories=[
                    Story(id="S01", title="x", acceptance=("a",)),
                ])
                runs = generate_runs(spec, ws, python_language(), _Cfg(), emit=lambda *a, **k: None)
        self.assertEqual(runs, [])


class TestRunScripted(unittest.TestCase):
    def test_passing_run(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "tool.py", '''\
                import sys
                if len(sys.argv) > 1:
                    print(f"hello {sys.argv[1]}")
            ''')
            run = ScriptedRun(
                story_id="S01", title="greet", priority="must",
                argv=("python3", "tool.py", "world"),
                expect_exit_code=0,
                expect_stdout_contains=("hello world",),
            )
            fail = run_scripted(run, ws)
            self.assertIsNone(fail)

    def test_wrong_exit_code(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "tool.py", "import sys; sys.exit(2)")
            run = ScriptedRun(
                story_id="S01", title="exit zero", priority="must",
                argv=("python3", "tool.py"),
                expect_exit_code=0,
            )
            fail = run_scripted(run, ws)
            self.assertIsNotNone(fail)
            self.assertEqual(fail.failure_kind, "exit_code_mismatch")
            self.assertIn("got 2", fail.detail)

    def test_missing_substring(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "tool.py", "print('nothing here')")
            run = ScriptedRun(
                story_id="S01", title="contains 'success'", priority="must",
                argv=("python3", "tool.py"),
                expect_stdout_contains=("success",),
            )
            fail = run_scripted(run, ws)
            self.assertIsNotNone(fail)
            self.assertEqual(fail.failure_kind, "stdout_mismatch")

    def test_forbidden_substring(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "tool.py", "print('Traceback (most recent call last):')")
            run = ScriptedRun(
                story_id="S01", title="no traceback", priority="must",
                argv=("python3", "tool.py"),
                expect_stdout_not_contains=("Traceback",),
            )
            fail = run_scripted(run, ws)
            self.assertIsNotNone(fail)
            self.assertEqual(fail.failure_kind, "stdout_mismatch")

    def test_setup_chain_runs_first(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "tool.py", '''\
                import sys, os
                if sys.argv[1] == "init":
                    open(os.path.join(os.path.dirname(__file__), "state.txt"), "w").write("ready")
                elif sys.argv[1] == "check":
                    p = os.path.join(os.path.dirname(__file__), "state.txt")
                    print("OK" if os.path.exists(p) else "MISSING")
            ''')
            run = ScriptedRun(
                story_id="S01", title="stateful", priority="must",
                argv=("python3", "tool.py", "check"),
                expect_stdout_contains=("OK",),
                setup=(
                    ScriptedRun(
                        story_id="S01.setup", title="init",
                        priority="must",
                        argv=("python3", "tool.py", "init"),
                    ),
                ),
            )
            fail = run_scripted(run, ws)
            self.assertIsNone(fail, msg=str(fail))


if __name__ == "__main__":
    unittest.main()
