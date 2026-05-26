"""Tests for cadillac/surgical.py — stuck-loop targeted edits.

The parser is tested with the exact diagnostic line formats produced by
pyflakes / ruff / py_compile. End-to-end `surgical_fix` is tested against
a synthetic broken file that reproduces today's `clean_email` failure
mode, with a mocked LLM returning a known-good patch — the goal is to
verify the edit-apply-recheck plumbing, not the LLM's judgement.
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from cadillac.languages import python_language
from cadillac.surgical import (
    StaticError,
    _apply_edit,
    _build_surgical_prompt,
    _candidate_definitions,
    _extract_undefined_name,
    _file_imports,
    _read_window,
    _sibling_init_exports,
    parse_static_errors,
    surgical_fix,
)
from cadillac.validate import CheckResult


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


# ── Parser tests ─────────────────────────────────────────────────────────────


class TestParsePyflakes(unittest.TestCase):
    def test_clean_email_fingerprint(self):
        """The exact line from the 2026-05-18 build that we want to fix."""
        r = CheckResult(
            "static_names", False,
            output="services/auth_service.py:217:45: undefined name 'clean_email'",
            severity="error",
        )
        errors = parse_static_errors([r])
        self.assertEqual(len(errors), 1)
        e = errors[0]
        self.assertEqual(e.file, "services/auth_service.py")
        self.assertEqual(e.line, 217)
        self.assertEqual(e.col, 45)
        self.assertEqual(e.error_class, "undefined name")
        self.assertEqual(
            e.fingerprint,
            "static_names:services/auth_service.py:217:undefined name",
        )

    def test_multiple_errors_each_get_fingerprint(self):
        r = CheckResult(
            "static_names", False,
            output=(
                "a.py:10:5: undefined name 'foo'\n"
                "b.py:22:1: undefined name 'bar'"
            ),
            severity="error",
        )
        errors = parse_static_errors([r])
        self.assertEqual(len(errors), 2)
        fps = {e.fingerprint for e in errors}
        self.assertEqual(len(fps), 2)

    def test_skips_passed_checks(self):
        r_ok = CheckResult("static_names", True, output="all clean", severity="info")
        self.assertEqual(parse_static_errors([r_ok]), [])

    def test_skips_unrelated_checks(self):
        r = CheckResult("functional", False,
                         output="some.py:5:1: undefined name 'x'",
                         severity="error")
        self.assertEqual(parse_static_errors([r]), [])


class TestParseRuff(unittest.TestCase):
    def test_ruff_lint_error(self):
        r = CheckResult(
            "lint", False,
            output="app/main.py:10:5: F401 'os' imported but unused",
            severity="error",
        )
        errors = parse_static_errors([r])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].error_class, "F401")
        self.assertEqual(errors[0].check_name, "lint")


class TestParseSyntax(unittest.TestCase):
    def test_syntax_error_format(self):
        r = CheckResult(
            "syntax", False,
            output="broken.py:42: invalid syntax",
            severity="error",
        )
        errors = parse_static_errors([r])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].line, 42)
        self.assertEqual(errors[0].error_class, "syntax error")


# ── Window read ──────────────────────────────────────────────────────────────


class TestReadWindow(unittest.TestCase):
    def test_marks_target_line(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "f.py",
                "\n".join(f"line {i}" for i in range(1, 31)) + "\n")
            window = _read_window(ws, "f.py", line=15, radius=3)
            self.assertIn(">>>", window)
            self.assertIn("line 15", window)
            # Window covers 12-18 (radius 3, target 15)
            self.assertIn("line 12", window)
            self.assertIn("line 18", window)
            self.assertNotIn("line 5", window)

    def test_missing_file_returns_error_string(self):
        with tempfile.TemporaryDirectory() as ws:
            window = _read_window(ws, "missing.py", line=1)
            self.assertTrue(window.startswith("<could not read"))


# ── Apply edit ───────────────────────────────────────────────────────────────


class TestApplyEdit(unittest.TestCase):
    def test_unique_replacement(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "f.py", "alpha\nbeta\ngamma\n")
            ok, why = _apply_edit(ws, "f.py", "beta", "DELTA")
            self.assertTrue(ok, msg=why)
            with open(os.path.join(ws, "f.py")) as f:
                self.assertEqual(f.read(), "alpha\nDELTA\ngamma\n")

    def test_ambiguous_rejected(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "f.py", "x = 1\nx = 1\n")
            ok, why = _apply_edit(ws, "f.py", "x = 1", "x = 2")
            self.assertFalse(ok)
            self.assertIn("ambiguous", why)

    def test_missing_old_string_rejected(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "f.py", "hello\n")
            ok, why = _apply_edit(ws, "f.py", "missing", "x")
            self.assertFalse(ok)
            self.assertIn("not found", why)


# ── End-to-end surgical_fix with mocked LLM ──────────────────────────────────


class TestSurgicalFixEndToEnd(unittest.TestCase):
    def test_clean_email_fix(self):
        """Replays today's failure: a function uses `clean_email` without
        defining it, surgical mode rewrites the line, pyflakes goes green."""
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "services/auth_service.py", '''\
                class AuthService:
                    def __init__(self, repo):
                        self._user_repo = repo

                    def change_password(self, email, old_pw, new_pw):
                        """Stub method that reproduces the bug."""
                        user = self._user_repo.get_by_email(clean_email)
                        return user
            ''')
            error = StaticError(
                check_name="static_names",
                file="services/auth_service.py",
                line=7, col=45,
                error_class="undefined name",
                detail="services/auth_service.py:7:45: undefined name 'clean_email'",
            )
            llm_payload = (
                '{"old_string": "user = self._user_repo.get_by_email(clean_email)",'
                ' "new_string": "user = self._user_repo.get_by_email(email.strip().lower())",'
                ' "explanation": "use email parameter normalized like other methods do"}'
            )
            with patch("cadillac.engine.chat",
                        return_value={"role": "assistant",
                                       "content": llm_payload, "tool_calls": []}):
                ok = surgical_fix(error, ws, python_language(), _Cfg(),
                                   emit=lambda *a, **k: None)
            self.assertTrue(ok, "surgical_fix should clear the undefined name")
            with open(os.path.join(ws, "services/auth_service.py")) as f:
                text = f.read()
            self.assertNotIn("clean_email", text)
            self.assertIn("email.strip().lower()", text)

    def test_llm_returns_garbage_returns_false(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "f.py", "x = clean_email\n")
            error = StaticError(
                check_name="static_names", file="f.py", line=1, col=5,
                error_class="undefined name",
                detail="f.py:1:5: undefined name 'clean_email'",
            )
            with patch("cadillac.engine.chat",
                        return_value={"role": "assistant",
                                       "content": "not json at all",
                                       "tool_calls": []}):
                ok = surgical_fix(error, ws, python_language(), _Cfg(),
                                   emit=lambda *a, **k: None)
            self.assertFalse(ok)

    def test_llm_returns_noop_rejected(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "f.py", "x = clean_email\n")
            error = StaticError(
                check_name="static_names", file="f.py", line=1, col=5,
                error_class="undefined name",
                detail="f.py:1:5: undefined name 'clean_email'",
            )
            with patch("cadillac.engine.chat",
                        return_value={"role": "assistant",
                                       "content": '{"old_string": "x", "new_string": "x"}',
                                       "tool_calls": []}):
                ok = surgical_fix(error, ws, python_language(), _Cfg(),
                                   emit=lambda *a, **k: None)
            self.assertFalse(ok)


# ── Cross-module name resolution hints ───────────────────────────────────────


class TestNameResolutionHints(unittest.TestCase):
    """Surgical's earlier ceiling: the LLM saw `User` undefined but had no
    idea which file declared `class User`. These helpers close that gap by
    grepping the workspace for definitions and surfacing sibling __init__
    exports."""

    def test_extract_undefined_name(self):
        self.assertEqual(
            _extract_undefined_name("file.py:1:1: undefined name 'Foo'"),
            "Foo",
        )
        self.assertEqual(
            _extract_undefined_name("not the right shape"), None,
        )

    def test_candidate_definitions_finds_class(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "models/user.py", "class User:\n    pass\n")
            _mk(ws, "auth/service.py", "from typing import Optional\n")
            hits = _candidate_definitions(ws, "User")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0][0], "models/user.py")
            self.assertIn("class User", hits[0][1])

    def test_candidate_definitions_finds_function_and_constant(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "utils.py", "def helper():\n    return 1\n")
            _mk(ws, "constants.py", "MAX_RETRIES = 5\n")
            self.assertEqual(
                _candidate_definitions(ws, "helper")[0][0], "utils.py")
            self.assertEqual(
                _candidate_definitions(ws, "MAX_RETRIES")[0][0], "constants.py")

    def test_candidate_definitions_rejects_invalid_identifier(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(_candidate_definitions(ws, ""), [])
            self.assertEqual(_candidate_definitions(ws, "not-an-ident"), [])

    def test_file_imports(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "f.py", textwrap.dedent('''\
                """Module docstring."""
                from typing import Optional
                import os
                from .sibling import X

                def foo(): pass
            '''))
            block = _file_imports(ws, "f.py")
            self.assertIn("from typing import Optional", block)
            self.assertIn("import os", block)
            self.assertIn("from .sibling import X", block)

    def test_sibling_init_exports(self):
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "models/__init__.py", "from .user import User\n")
            _mk(ws, "models/user.py", "class User: pass\n")
            _mk(ws, "auth/__init__.py", "")
            _mk(ws, "auth/service.py", "x = 1\n")
            out = _sibling_init_exports(ws, "auth/service.py")
            self.assertIn("models/__init__.py", out)
            self.assertIn("from .user import User", out)

    def test_prompt_for_undefined_name_includes_hints(self):
        """Validates the user message contains candidates + imports + siblings."""
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "models/__init__.py", "from .user import User\n")
            _mk(ws, "models/user.py", "class User:\n    pass\n")
            _mk(ws, "auth/__init__.py", "")
            _mk(ws, "auth/service.py", textwrap.dedent('''\
                from typing import Optional


                class Service:
                    def look(self, e):
                        return User(e)
            '''))
            error = StaticError(
                check_name="static_names", file="auth/service.py",
                line=6, col=20,
                error_class="undefined name",
                detail="auth/service.py:6:20: undefined name 'User'",
            )
            window = _read_window(ws, "auth/service.py", 6)
            prompt = _build_surgical_prompt(error, window, ws)
            self.assertIn("CURRENT IMPORTS", prompt)
            self.assertIn("from typing import Optional", prompt)
            self.assertIn("CANDIDATE DEFINITIONS of 'User'", prompt)
            self.assertIn("models/user.py", prompt)
            self.assertIn("SIBLING PACKAGE EXPORTS", prompt)

    def test_prompt_for_non_undefined_name_skips_hints(self):
        """Lint / syntax errors don't get the heavy hint block — only the
        20-line window, to keep prompt size minimal for cases that don't
        need cross-module reasoning."""
        with tempfile.TemporaryDirectory() as ws:
            _mk(ws, "f.py", "x = 1\n")
            error = StaticError(
                check_name="lint", file="f.py", line=1, col=1,
                error_class="F401",
                detail="f.py:1:1: F401 'os' imported but unused",
            )
            window = _read_window(ws, "f.py", 1)
            prompt = _build_surgical_prompt(error, window, ws)
            self.assertNotIn("CANDIDATE DEFINITIONS", prompt)
            self.assertNotIn("SIBLING PACKAGE EXPORTS", prompt)


if __name__ == "__main__":
    unittest.main()
