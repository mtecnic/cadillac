"""Tests for the library runtime runner (cadillac/runtime/library_runner.py)."""

from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from cadillac.languages import python_language
from cadillac.runtime.library_runner import (
    UsageExample,
    generate_examples,
    run_example,
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


class TestGenerateExamplesParse(unittest.TestCase):
    def test_parses_clean(self):
        payload = json.dumps({"examples": [
            {"story_id": "S01", "title": "import + call",
             "priority": "must", "language": "python",
             "code": "from mylib import foo\nassert foo() == 42\n"},
        ]})
        with patch("cadillac.engine.chat",
                    return_value={"role": "assistant",
                                   "content": payload, "tool_calls": []}):
            with tempfile.TemporaryDirectory() as ws:
                _mk(ws, "mylib/__init__.py", "from .core import foo\n")
                _mk(ws, "mylib/core.py", "def foo(): return 42\n")
                spec = Spec(task="t", stories=[
                    Story(id="S01", title="lib works", acceptance=("a",)),
                ])
                exes = generate_examples(spec, ws, python_language(), _Cfg(),
                                          emit=lambda *a, **k: None)
        self.assertEqual(len(exes), 1)
        self.assertEqual(exes[0].language, "python")

    def test_rejects_unknown_language(self):
        payload = json.dumps({"examples": [
            {"story_id": "S01", "title": "x", "priority": "must",
             "language": "haskell", "code": "main = putStrLn \"hi\""},
        ]})
        with patch("cadillac.engine.chat",
                    return_value={"role": "assistant",
                                   "content": payload, "tool_calls": []}):
            with tempfile.TemporaryDirectory() as ws:
                spec = Spec(task="t", stories=[
                    Story(id="S01", title="x", acceptance=("a",)),
                ])
                exes = generate_examples(spec, ws, python_language(), _Cfg(),
                                          emit=lambda *a, **k: None)
        self.assertEqual(exes, [])


class TestRunExample(unittest.TestCase):
    def test_passing_python_snippet(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "mylib/__init__.py", "from .core import foo\n")
            _mk(ws, "mylib/core.py", "def foo(): return 42\n")
            ex = UsageExample(
                story_id="S01", title="foo returns 42", priority="must",
                language="python",
                code="from mylib import foo\nassert foo() == 42\n",
            )
            fail = run_example(ex, ws)
            self.assertIsNone(fail, msg=str(fail))

    def test_failing_assertion(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "mylib/__init__.py", "from .core import foo\n")
            _mk(ws, "mylib/core.py", "def foo(): return 7\n")
            ex = UsageExample(
                story_id="S01", title="foo returns 42", priority="must",
                language="python",
                code="from mylib import foo\nassert foo() == 42, f'got {foo()}'\n",
            )
            fail = run_example(ex, ws)
            self.assertIsNotNone(fail)
            self.assertEqual(fail.failure_kind, "assertion")

    def test_import_error_classified(self):
        with tempfile.TemporaryDirectory() as ws:
            ex = UsageExample(
                story_id="S01", title="missing module", priority="must",
                language="python",
                code="import not_a_real_module\nassert True\n",
            )
            fail = run_example(ex, ws)
            self.assertIsNotNone(fail)
            self.assertEqual(fail.failure_kind, "import_error")


if __name__ == "__main__":
    unittest.main()
