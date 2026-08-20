"""Pins the anti-patterns promoted from memory.jsonl's recurring failures.

A lesson in memory.jsonl only helps when `recall()` happens to surface it —
it is filtered by tag and word-overlap against the current task. The clusters
below recurred often enough across 492 lessons that leaving them to chance was
wasting rounds, so they were promoted into quality.py, which is in EVERY prompt.

Clusters mined from the 309 `error_pattern` lessons (2026-08-20):

    111  imports / module resolution   <- largest by far
    108  tests / fixtures
     57  types / validation
     47  entry point / packaging
     41  sqlite / db concurrency

and within those, the specific recurring shapes:

     19  missing `__init__.py` -> ModuleNotFoundError
     12  hardcoded ports/paths instead of injected config
     12  incomplete `index.ts` barrel exports (TS)
      4  missing `if __name__ == "__main__":` on the entry point
      3  empty module directories (also failed a real build outright)

These tests do not assert wording — they assert the RULE is present, so the
text can be reworded without breaking, but silently dropping a hard-won lesson
fails.
"""

import unittest

from cadillac import quality
from cadillac.languages import python_language, typescript_language
from cadillac.prompts import build_build_prompt


class TestPythonAntiPatterns(unittest.TestCase):
    def setUp(self):
        self.text = quality.ANTI_PATTERNS.lower()

    def test_init_py_required_in_every_package_dir(self):
        """The largest cluster: 19 lessons, all ModuleNotFoundError."""
        self.assertIn("__init__.py", self.text)
        self.assertIn("nested", self.text)

    def test_reexport_requires_the_file_to_exist(self):
        self.assertIn("re-export", self.text)
        self.assertIn("implementation file first", self.text)

    def test_empty_module_directories_are_forbidden(self):
        """Failed a real build outright: `functional: Empty module directories`."""
        self.assertIn("empty", self.text)
        self.assertIn("delete the directory", self.text)

    def test_entry_point_needs_a_main_guard(self):
        self.assertIn('__name__ == "__main__"', quality.ANTI_PATTERNS)

    def test_no_hardcoded_ports_or_paths(self):
        self.assertIn("hardcode", self.text)
        self.assertIn("os.environ.get", self.text)

    def test_sqlite_has_no_select_for_update(self):
        """Seen live on the job-queue build: the model reached for FOR UPDATE,
        which SQLite silently does not support."""
        self.assertIn("for update", self.text)
        self.assertIn("begin immediate", self.text)

    def test_async_tests_need_pytest_asyncio(self):
        self.assertIn("pytest-asyncio", self.text)
        self.assertIn("asyncio_mode", self.text)

    def test_preexisting_rules_survived(self):
        """Promotion must not have displaced what was already there."""
        for rule in ("shadows a python stdlib module", "fts5",
                     "try/except importerror", "global singleton"):
            self.assertIn(rule, self.text, f"pre-existing rule lost: {rule}")


class TestTypeScriptAntiPatterns(unittest.TestCase):
    def setUp(self):
        self.text = quality.TS_ANTI_PATTERNS.lower()

    def test_barrel_exports_must_be_complete(self):
        """12 lessons — a missing export is invisible to tsc in the defining
        file and only fails at the import site."""
        self.assertIn("index.ts", self.text)
        self.assertIn("re-export every", self.text)

    def test_app_listen_must_be_guarded(self):
        self.assertIn("require.main === module", quality.TS_ANTI_PATTERNS)

    def test_no_hardcoded_port_or_path(self):
        self.assertIn("process.env.port", self.text)

    def test_preexisting_rules_survived(self):
        for rule in ('"type": "module"', "rootdir", "ts-jest"):
            self.assertIn(rule, self.text, f"pre-existing rule lost: {rule}")


class TestPromptsStillRender(unittest.TestCase):
    """These strings are interpolated with .format(), so an unescaped brace in a
    new rule breaks every build for that language. The TS rules contain literal
    JS braces, which is exactly where that goes wrong."""

    def test_python_build_prompt_renders(self):
        text = build_build_prompt(
            entry_point="main.py", manifest_summary="", progress_context="",
            validation_failures="", lessons_text="", lang=python_language())
        self.assertIn("__init__.py", text)

    def test_typescript_build_prompt_renders(self):
        text = build_build_prompt(
            entry_point="src/index.ts", manifest_summary="", progress_context="",
            validation_failures="", lessons_text="", lang=typescript_language())
        self.assertIn("index.ts", text)

    def test_braces_in_ts_rules_are_escaped(self):
        """A single `{` in these templates raises KeyError at format() time."""
        rendered = quality.TS_ANTI_PATTERNS.format()
        self.assertIn("require.main === module", rendered)


class TestSizeStaysReasonable(unittest.TestCase):
    """Every rule here costs tokens on EVERY llm call, so growth is a real
    trade-off, not free. This is a tripwire, not a hard limit — raise it
    deliberately if a cluster genuinely earns the space."""

    def test_python_anti_patterns_bounded(self):
        self.assertLess(len(quality.ANTI_PATTERNS), 6000,
                        "ANTI_PATTERNS is growing unchecked; prune before adding")

    def test_ts_anti_patterns_bounded(self):
        self.assertLess(len(quality.TS_ANTI_PATTERNS), 6000,
                        "TS_ANTI_PATTERNS is growing unchecked; prune before adding")


if __name__ == "__main__":
    unittest.main()
