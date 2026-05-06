"""Tests for adversarial test generation (Phase 1.3 of weakness roadmap).

We can't fully test the LLM call without burning real LLM time. The tests
here cover:
  - Objective extraction from plan.json + contracts.json + architecture.md
  - Skipping when no objectives are present (refuses to write blind tests)
  - Code-block extraction from LLM output (fenced and unfenced)
  - Pytest output parsing
  - Vitest output parsing

The full LLM-driven flow is covered manually by running a real build with
adversarial enabled; we don't try to mock chat() here because the value of
the feature lives in the prompt+model interaction, not the plumbing.
"""

import json
import os
import tempfile
import unittest

from cadillac.adversarial import (
    AdversarialResult,
    ProjectObjectives,
    _extract_code_block,
    _extract_objectives,
    _parse_pytest_summary,
    _parse_vitest_summary,
    _extract_pytest_failures,
    _extract_vitest_failures,
    _pick_targets,
    run_adversarial_tests,
)
from cadillac.languages import python_language, react_language


def _write(td: str, rel: str, content: str) -> None:
    path = os.path.join(td, rel)
    os.makedirs(os.path.dirname(path) or td, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


class TestObjectiveExtraction(unittest.TestCase):
    def test_extracts_task_and_endpoints(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "plan.json", json.dumps({
                "task": "Build a bookmark manager with auth",
                "entry_point": "backend/app.py",
                "constraints": ["MUST persist bookmarks across restarts",
                                "MUST require auth on /api/bookmarks"],
            }))
            _write(td, "contracts.json", json.dumps({
                "endpoints": [
                    {"name": "register", "method": "POST",
                     "path": "/api/auth/register",
                     "module": "auth", "consumed_by": ["pages"]},
                    {"name": "list_bookmarks", "method": "GET",
                     "path": "/api/bookmarks",
                     "module": "bookmarks", "consumed_by": ["pages"]},
                ],
            }))
            obj = _extract_objectives(td)
            self.assertEqual(obj.task_text, "Build a bookmark manager with auth")
            self.assertEqual(obj.entry_point, "backend/app.py")
            self.assertEqual(len(obj.endpoints), 2)
            self.assertEqual(len(obj.constraints), 2)
            summary = obj.objectives_summary()
            self.assertTrue(any("/api/auth/register" in s for s in summary))
            self.assertTrue(any("/api/bookmarks" in s for s in summary))
            self.assertTrue(any("MUST persist" in s for s in summary))

    def test_extracts_architecture_requirements_only(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "architecture.md", (
                "# Project\n"
                "## Overview\n"
                "Some implementation prose to ignore.\n"
                "## Requirements\n"
                "- The user can register\n"
                "- The user can list bookmarks\n"
                "## Internal Notes\n"
                "Implementation details go here, ignore me.\n"
            ))
            obj = _extract_objectives(td)
            self.assertIn("Requirements", obj.architecture_excerpt)
            self.assertIn("user can register", obj.architecture_excerpt)
            self.assertNotIn("Implementation details", obj.architecture_excerpt)

    def test_no_plan_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            obj = _extract_objectives(td)
            self.assertEqual(obj.task_text, "")
            self.assertEqual(obj.endpoints, [])


class TestRefusesWithoutObjectives(unittest.TestCase):
    """A deliberate design choice: with no plan or contracts, the adversarial
    pass refuses rather than writing blind edge-case tests. That was the
    user's exact pushback during 1.3 design — adversarial tests must trace
    to declared running objectives, not just probe in the abstract."""

    def test_skips_with_no_objectives(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "src/m.py", "def f(x):\n    return x + 1\n")
            # No plan.json, no contracts.json, no architecture.md
            class _FakeCfg:
                pass
            result = run_adversarial_tests(td, python_language(), _FakeCfg(),
                                           lambda *a, **kw: None)
            self.assertTrue(result.passed)
            self.assertIn("can't establish what this program is for",
                          result.skipped_reason)


class TestPickTargets(unittest.TestCase):
    def test_skips_test_files_init_main(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "src/logic.py",
                   "def compute_total(items):\n    return sum(items)\n" * 20)
            _write(td, "src/__init__.py", "")
            _write(td, "src/main.py", "def main():\n    pass\n")
            _write(td, "tests/test_logic.py",
                   "from src.logic import compute_total\n"
                   "def test_basic():\n    assert compute_total([1,2]) == 3\n")
            targets = _pick_targets(td, python_language())
            self.assertTrue(any("logic.py" in t for t in targets))
            self.assertFalse(any("test_logic.py" in t for t in targets))
            self.assertFalse(any("__init__.py" in t for t in targets))
            self.assertFalse(any("main.py" in t for t in targets))

    def test_skips_node_modules(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "node_modules/dep/index.ts",
                   "export function leak() { return 'x'; }\n" * 20)
            _write(td, "src/feature.ts",
                   "export function compute(items: number[]) { "
                   "return items.reduce((a,b)=>a+b, 0); }\n" * 20)
            targets = _pick_targets(td, react_language())
            self.assertTrue(any("feature.ts" in t for t in targets))
            self.assertFalse(any("node_modules" in t for t in targets))


class TestCodeBlockExtraction(unittest.TestCase):
    def test_unwraps_python_fence(self):
        raw = "```python\nimport pytest\n\ndef test_x():\n    assert True\n```"
        self.assertEqual(
            _extract_code_block(raw, python_language()),
            "import pytest\n\ndef test_x():\n    assert True",
        )

    def test_unwraps_typescript_fence(self):
        raw = "```ts\nimport { describe } from 'vitest';\n```"
        self.assertEqual(
            _extract_code_block(raw, react_language()),
            "import { describe } from 'vitest';",
        )

    def test_passes_through_unfenced(self):
        raw = "import pytest\ndef test_x():\n    pass"
        self.assertEqual(_extract_code_block(raw, python_language()), raw)


class TestPytestParsing(unittest.TestCase):
    def test_passes_only(self):
        out = "...\n====== 5 passed in 0.04s ======\n"
        p, f = _parse_pytest_summary(out)
        self.assertEqual((p, f), (5, 0))

    def test_mixed(self):
        out = "FAILED tests/test_x.py::test_a - AssertionError\n" \
              "====== 2 failed, 3 passed in 0.04s ======\n"
        p, f = _parse_pytest_summary(out)
        self.assertEqual((p, f), (3, 2))
        failures = _extract_pytest_failures(out)
        self.assertEqual(len(failures), 1)
        self.assertIn("test_a", failures[0])

    def test_no_summary(self):
        # Empty output (e.g., pytest crashed before running)
        p, f = _parse_pytest_summary("collection failure")
        self.assertEqual((p, f), (0, 0))


class TestVitestParsing(unittest.TestCase):
    def test_passes_only(self):
        out = "Test Files  1 passed (1)\n     Tests  4 passed (4)\n"
        p, f = _parse_vitest_summary(out)
        self.assertEqual(p, 4)
        self.assertEqual(f, 0)

    def test_mixed(self):
        out = ("FAIL  src/a.test.ts > group > test name\n"
               "  Tests  2 failed | 3 passed (5)\n")
        p, f = _parse_vitest_summary(out)
        self.assertEqual(p, 3)
        self.assertEqual(f, 2)


if __name__ == "__main__":
    unittest.main()
