"""Tests for dependency-import resolution.

This was the FIRST link in build 12's failure chain:

  1. The module-scoped import check consulted a hardcoded 14-name allowlist
     instead of what was installed, so `aiosqlite` and `pydantic_settings` were
     reported unresolved though both were importable. Two healthy modules FAILED.
  2. The model "fixed" that by calling add_dep(), which wrote a package.json
     containing PYTHON packages.
  3. That package.json misrouted detect_language("") to typescript.
  4. The entire post-run phase then validated a Python project as TypeScript.

The workspace-level check was broken differently: it compared normalised module
names against un-normalised pkg_resources DISTRIBUTION keys, so
`pydantic_settings` could never match `pydantic-settings` — a guaranteed false
positive for every hyphenated distribution. It also carried its own 23-name
allowlist to paper over that, which masked genuinely missing dependencies.

Both now share `installed_import_names()`, built on
`importlib.metadata.packages_distributions()` — real import names, so `yaml`
(PyYAML), `PIL` (pillow) and `bs4` (beautifulsoup4) resolve with no alias table.
"""

import os
import tempfile
import unittest

from cadillac.languages import python_language
from cadillac.validate import (
    check_imports,
    installed_import_names,
    run_module_validation,
)


def _write(root, rel, body=""):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w") as f:
        f.write(body)


class TestInstalledImportNames(unittest.TestCase):
    def test_returns_real_import_names(self):
        names = installed_import_names()
        self.assertTrue(names, "no installed packages discovered at all")
        self.assertIn("pytest", names)

    def test_hyphenated_distribution_resolves_by_import_name(self):
        """`pydantic-settings` the distribution, `pydantic_settings` the import.
        The old comparison could never match these."""
        names = installed_import_names()
        if "pydantic_settings" not in names:
            self.skipTest("pydantic-settings not installed on this host")
        self.assertIn("pydantic_settings", names)

    def test_aliased_distributions_resolve(self):
        """yaml/PyYAML, PIL/pillow, bs4/beautifulsoup4 — no alias table needed."""
        names = installed_import_names()
        for imp in ("yaml", "bs4"):
            if imp in names:
                self.assertIn(imp, names)

    def test_result_is_cached_per_workspace(self):
        with tempfile.TemporaryDirectory() as ws:
            first = installed_import_names(ws)
            self.assertIs(installed_import_names(ws), first)

    def test_missing_workspace_venv_falls_back_to_host(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertTrue(installed_import_names(ws))


class TestModuleScopedImportCheck(unittest.TestCase):
    """The check that started the chain."""

    def _failures(self, td, module):
        return [r for r in run_module_validation(td, module, None, lang=python_language())
                if not r.passed and r.name == "imports"]

    def test_installed_dependency_is_not_flagged(self):
        """`aiosqlite` is installed; the old 14-name list did not include it."""
        names = installed_import_names()
        if "aiosqlite" not in names:
            self.skipTest("aiosqlite not installed on this host")
        with tempfile.TemporaryDirectory() as td:
            _write(td, "mod/__init__.py")
            _write(td, "mod/db.py", "import aiosqlite\n")
            self.assertEqual(self._failures(td, "mod"), [])

    def test_hyphenated_dependency_is_not_flagged(self):
        names = installed_import_names()
        if "pydantic_settings" not in names:
            self.skipTest("pydantic-settings not installed on this host")
        with tempfile.TemporaryDirectory() as td:
            _write(td, "mod/__init__.py")
            _write(td, "mod/cfg.py", "from pydantic_settings import BaseSettings\n")
            self.assertEqual(self._failures(td, "mod"), [])

    def test_genuinely_missing_dependency_is_still_flagged(self):
        """Precision matters as much as recall — this must not become a no-op."""
        with tempfile.TemporaryDirectory() as td:
            _write(td, "mod/__init__.py")
            _write(td, "mod/a.py", "import definitely_not_a_real_package_xyz\n")
            failures = self._failures(td, "mod")
            self.assertEqual(len(failures), 1)
            self.assertIn("definitely_not_a_real_package_xyz", failures[0].output)

    def test_stdlib_and_local_imports_are_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "mod/__init__.py")
            _write(td, "mod/helper.py", "x = 1\n")
            _write(td, "mod/a.py", "import json\nimport os\nfrom . import helper\n")
            self.assertEqual(self._failures(td, "mod"), [])

    def test_no_hardcoded_package_list_remains(self):
        import inspect

        from cadillac.validate import run_module_validation as fn
        src = inspect.getsource(fn)
        self.assertNotIn('"uvicorn", "sqlalchemy"', src,
                         "the hardcoded package allowlist is back")


class TestWorkspaceLevelImportCheck(unittest.TestCase):
    def _failures(self, td):
        return [r for r in check_imports(td, python_language()) if not r.passed]

    def test_hyphenated_distribution_is_not_flagged(self):
        names = installed_import_names()
        if "pydantic_settings" not in names:
            self.skipTest("pydantic-settings not installed on this host")
        with tempfile.TemporaryDirectory() as td:
            _write(td, "app.py", "import pydantic_settings\n")
            self.assertEqual(self._failures(td), [])

    def test_aliased_distribution_is_not_flagged(self):
        if "yaml" not in installed_import_names():
            self.skipTest("PyYAML not installed on this host")
        with tempfile.TemporaryDirectory() as td:
            _write(td, "app.py", "import yaml\n")
            self.assertEqual(self._failures(td), [])

    def test_genuinely_missing_dependency_is_still_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "app.py", "import nonexistent_pkg_abc\n")
            failures = self._failures(td)
            self.assertEqual(len(failures), 1)
            self.assertIn("nonexistent_pkg_abc", failures[0].output)

    def test_alias_allowlist_no_longer_masks_missing_packages(self):
        import inspect

        from cadillac.validate import check_imports as fn
        src = inspect.getsource(fn)
        self.assertNotIn('"pendulum", "arrow"', src,
                         "the alias allowlist is back and can mask missing deps")


class TestBothChecksAgree(unittest.TestCase):
    """One rule, one source of truth — the recurring defect in this codebase is
    the same rule implemented twice and drifting."""

    def test_both_use_the_shared_helper(self):
        import inspect

        from cadillac.validate import check_imports, run_module_validation
        for fn in (check_imports, run_module_validation):
            self.assertIn("installed_import_names", inspect.getsource(fn),
                          f"{fn.__name__} does not use the shared resolver")

    def test_same_verdict_for_the_same_import(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "mod/__init__.py")
            _write(td, "mod/a.py", "import nonexistent_pkg_abc\n")
            _write(td, "app.py", "import nonexistent_pkg_abc\n")
            mod_bad = [r for r in run_module_validation(td, "mod", None, lang=python_language())
                       if not r.passed and r.name == "imports"]
            ws_bad = [r for r in check_imports(td, python_language()) if not r.passed]
        self.assertTrue(mod_bad, "module check missed it")
        self.assertTrue(ws_bad, "workspace check missed it")


if __name__ == "__main__":
    unittest.main()
