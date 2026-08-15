"""Tests for the two defects that invalidated build 12's post-run phase.

Build 12 (a 21-file Python FastAPI service, zero .ts files) finished with:

    [FAIL] imports: node_modules missing — run 'npm install'
    [PASS] syntax: No tsconfig.json found, skipping
    [PASS] operational: runtime gates: typescript backend, skipped

Every validation after run() executed as TypeScript. The causal chain:

  1. The module import check falsely flagged `aiosqlite` (it consults a
     hardcoded 14-package allowlist, not what is installed).
  2. The model "fixed" that by calling add_dep(), which wrote a package.json
     containing a PYTHON package: {"aiosqlite": "==0.19.0"}.
  3. `iterate()` calls `detect_language(instruction or "", workspace)` — with
     an empty string — and the workspace fallback trusted package.json's mere
     existence, so it returned typescript.
  4. Auto-iterate then ran npm/tsc/.test.ts logic against Python source, and
     the failure count went UP (2 -> 3). I initially mistook that for the model
     degrading the code; it was the harness switching languages underneath it.

Separately, the same log showed:

    pytest: "file or directory not found ... no tests ran in 0.09s", exit 0
    -> "[ITERATE/services] Tests pass!"

Zero tests ran and the loop declared success.
"""

import os
import tempfile
import unittest

from cadillac.engine import _tests_actually_passed
from cadillac.languages import detect_language


def _write(root, rel, body=""):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w") as f:
        f.write(body)


class TestLanguageFallbackPrefersTheCode(unittest.TestCase):
    """A stray manifest must not outvote the source tree."""

    def test_python_tree_with_a_stray_package_json(self):
        """The exact build-12 shape: many .py files, a bogus package.json."""
        with tempfile.TemporaryDirectory() as td:
            for i in range(8):
                _write(td, f"pkg/mod{i}.py", "x = 1\n")
            _write(td, "package.json", '{"dependencies":{"aiosqlite":"==0.19.0"}}')
            self.assertEqual(detect_language("", td).name, "python")

    def test_real_typescript_tree_still_detects_typescript(self):
        with tempfile.TemporaryDirectory() as td:
            for i in range(6):
                _write(td, f"src/mod{i}.ts", "export const x = 1;\n")
            _write(td, "package.json", "{}")
            self.assertEqual(detect_language("", td).name, "typescript")

    def test_package_json_still_wins_when_there_is_no_source(self):
        """Empty scaffold: the manifest is the only evidence available."""
        with tempfile.TemporaryDirectory() as td:
            _write(td, "package.json", "{}")
            self.assertEqual(detect_language("", td).name, "typescript")

    def test_empty_workspace_defaults_to_python(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_language("", td).name, "python")

    def test_task_text_still_takes_priority(self):
        """Workspace evidence is a FALLBACK, not an override."""
        with tempfile.TemporaryDirectory() as td:
            for i in range(6):
                _write(td, f"pkg/mod{i}.py", "x = 1\n")
            self.assertEqual(
                detect_language("Build a React dashboard", td).name, "react")

    def test_generated_trees_do_not_swamp_the_count(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "app/main.py", "x = 1\n")
            _write(td, "app/util.py", "x = 1\n")
            for i in range(40):
                _write(td, f"node_modules/dep{i}/index.js", "module.exports={}\n")
            self.assertEqual(detect_language("", td).name, "python")

    def test_mixed_tree_is_ambiguous_and_falls_through(self):
        """A genuine 50/50 split must not be decided by file count."""
        with tempfile.TemporaryDirectory() as td:
            for i in range(3):
                _write(td, f"a/m{i}.py", "x = 1\n")
                _write(td, f"b/m{i}.ts", "export const x = 1;\n")
            # No majority -> falls through to package.json / python default.
            self.assertEqual(detect_language("", td).name, "python")


class TestIteratePassesRealLanguage(unittest.TestCase):
    def test_iterate_does_not_rely_on_an_empty_instruction(self):
        """Pin the call that caused it, so a refactor cannot silently restore
        the empty-string detection without the workspace fallback."""
        import inspect

        from cadillac.engine import iterate
        src = inspect.getsource(iterate)
        self.assertIn("detect_language", src)
        # The workspace must be supplied — that is what makes the fallback work.
        i = src.index("detect_language")
        self.assertIn("workspace", src[i:i + 80])


class TestTestsActuallyPassed(unittest.TestCase):
    """Exit code 0 is not evidence that tests ran."""

    def test_the_exact_build12_case_is_not_a_pass(self):
        result = {
            "exit_code": 0,
            "stdout": "ERROR: file or directory not found: "
                      "services/test_services.py\n\n\nno tests ran in 0.09s\n",
            "stderr": "",
        }
        self.assertFalse(_tests_actually_passed("python3 -m pytest -q services/", result))

    def test_no_tests_collected_is_not_a_pass(self):
        result = {"exit_code": 0, "stdout": "no tests collected", "stderr": ""}
        self.assertFalse(_tests_actually_passed("pytest", result))

    def test_real_pass_is_a_pass(self):
        result = {"exit_code": 0, "stdout": "12 passed in 1.20s", "stderr": ""}
        self.assertTrue(_tests_actually_passed("python3 -m pytest -q", result))

    def test_failures_are_not_a_pass(self):
        result = {"exit_code": 1, "stdout": "2 failed, 3 passed", "stderr": ""}
        self.assertFalse(_tests_actually_passed("pytest", result))

    def test_exit_zero_with_failures_in_output_is_not_a_pass(self):
        result = {"exit_code": 0, "stdout": "1 failed, 3 passed", "stderr": ""}
        self.assertFalse(_tests_actually_passed("pytest", result))

    def test_non_test_command_is_never_a_pass(self):
        result = {"exit_code": 0, "stdout": "12 passed", "stderr": ""}
        self.assertFalse(_tests_actually_passed("ls -la", result))

    def test_jest_and_vitest_are_recognised(self):
        result = {"exit_code": 0, "stdout": "Tests: 4 passed, 4 total", "stderr": ""}
        self.assertTrue(_tests_actually_passed("npx jest", result))
        self.assertTrue(_tests_actually_passed("npx vitest run", result))

    def test_malformed_result_is_not_a_pass(self):
        self.assertFalse(_tests_actually_passed("pytest", None))
        self.assertFalse(_tests_actually_passed("pytest", "ok"))


class TestAllThreeSitesShareThePredicate(unittest.TestCase):
    """The three call sites had DRIFTED — BUILD required positive evidence
    while MODULE and ITERATE checked only the exit code. Same-rule-twice is
    the defect shape that has recurred throughout this work."""

    def test_no_site_checks_the_exit_code_alone(self):
        import inspect

        from cadillac.engine import run
        src = inspect.getsource(run)
        self.assertNotIn('cmd_result.get("exit_code") == 0)', src,
                         "a Tests-pass site is still trusting the exit code alone")

    def test_every_tests_pass_emit_is_guarded_by_the_predicate(self):
        import re

        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "engine.py")) as f:
            src = f.read()
        for m in re.finditer(r'Tests pass!', src):
            window = src[max(0, m.start() - 400):m.start()]
            self.assertIn("_tests_actually_passed", window,
                          "a 'Tests pass!' emit is not guarded by the shared predicate")


if __name__ == "__main__":
    unittest.main()
