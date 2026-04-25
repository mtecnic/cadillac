"""Tests for cadillac.scratch — within-build scratchpad."""

import os
import tempfile
import unittest

from cadillac.scratch import Scratch, VALID_CATEGORIES


class TestScratch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_path_workspace_root(self):
        s = Scratch(self.workspace)
        self.assertEqual(s.path(), os.path.join(self.workspace, ".cadillac", "scratch.md"))

    def test_path_module_scoped(self):
        s = Scratch(self.workspace, module="auth")
        self.assertEqual(s.path(), os.path.join(self.workspace, ".cadillac", "auth.scratch.md"))

    def test_path_module_with_slash(self):
        s = Scratch(self.workspace, module="core/storage")
        self.assertEqual(s.path(), os.path.join(self.workspace, ".cadillac", "core__storage.scratch.md"))

    def test_append_creates_file_with_section_header(self):
        s = Scratch(self.workspace)
        result = s.append("tried_failed", "boom", phase="BUILD", round_num=2)
        self.assertEqual(result, "ok")
        self.assertTrue(os.path.exists(s.path()))
        text = s.read()
        self.assertIn("## Tried & Failed", text)
        self.assertIn("[BUILD R2] boom", text)

    def test_append_invalid_category(self):
        s = Scratch(self.workspace)
        r = s.append("nope", "stuff")
        self.assertTrue(r.startswith("error: category"))

    def test_append_empty_content(self):
        s = Scratch(self.workspace)
        self.assertTrue(s.append("reminder", "   ").startswith("error"))

    def test_append_truncates_long_content(self):
        s = Scratch(self.workspace)
        s.append("reminder", "x" * 500, phase="BUILD", round_num=1)
        text = s.read()
        # 200-char cap + " ..." marker
        self.assertIn("xxxxx...", text)
        # the line shouldn't be obscenely long
        line = [ln for ln in text.splitlines() if ln.startswith("- [BUILD R1]")][0]
        self.assertLess(len(line), 250)

    def test_append_dedups_recent_duplicate(self):
        s = Scratch(self.workspace)
        r1 = s.append("tried_failed", "import error in auth/__init__.py", phase="BUILD", round_num=1)
        r2 = s.append("tried_failed", "import error in auth/__init__.py", phase="BUILD", round_num=2)
        self.assertEqual(r1, "ok")
        self.assertTrue(r2.startswith("skipped"))

    def test_append_does_not_dedup_distinct_entries(self):
        s = Scratch(self.workspace)
        s.append("tried_failed", "import error in auth/__init__.py", phase="BUILD", round_num=1)
        r = s.append("tried_failed", "totally separate database connection failure problem", phase="BUILD", round_num=2)
        self.assertEqual(r, "ok")

    def test_read_truncates_to_max_chars(self):
        s = Scratch(self.workspace)
        for i in range(50):
            s.append("reminder", f"distinct unique reminder number {i} for content " * 3,
                     phase="BUILD", round_num=i)
        text = s.read(max_chars=500)
        self.assertLessEqual(len(text), 500 + 100)
        # Most-recent bias: latest entries (high round nums) should be present
        self.assertIn("truncated", text.lower())

    def test_read_dependencies_concatenates_module_scratches(self):
        a = Scratch(self.workspace, module="auth")
        b = Scratch(self.workspace, module="storage")
        a.append("working_pattern", "use bcrypt for hashing", phase="BUILD", round_num=1)
        b.append("working_pattern", "sqlite3 with WAL mode", phase="BUILD", round_num=1)
        out = Scratch.read_dependencies(self.workspace, ["auth", "storage"])
        self.assertIn("From auth/scratch.md", out)
        self.assertIn("From storage/scratch.md", out)
        self.assertIn("bcrypt", out)
        self.assertIn("sqlite3", out)

    def test_read_dependencies_empty_for_missing_modules(self):
        out = Scratch.read_dependencies(self.workspace, ["does-not-exist"])
        self.assertEqual(out, "")

    def test_read_empty_file(self):
        s = Scratch(self.workspace)
        self.assertEqual(s.read(), "")

    def test_valid_categories_constant(self):
        self.assertEqual(set(VALID_CATEGORIES), {"tried_failed", "working_pattern", "reminder"})


if __name__ == "__main__":
    unittest.main()
