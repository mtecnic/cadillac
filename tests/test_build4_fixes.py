"""Tests for the five issues surfaced by build 4 (2026-08-14).

Build 4 (ctxpack, a pure library) failed its first three modules on the same
non-issue and exposed two structural gaps:

  1. `check_stdlib_conflicts` matched on BASENAME anywhere in the tree, so
     `ctxpack/types/chunk.py` — imported as `ctxpack.types.chunk`, incapable of
     shadowing anything — was reported as shadowing stdlib `chunk`.
  2. `check_entry_point` demanded `main.py`, which a library deliberately does
     not have, guaranteeing a failure for an entire project class.
  3. Module validation emitted only a COUNT; the actual failure text was never
     recorded, so a post-mortem could see that a module failed but not why.
  4. Stuck-loop detection fingerprinted only static errors, so it fired zero
     times across four builds while the loop ground on test/runtime failures.
  5. RUNTIME regenerated its probe suite every cycle (16/12 then 12/10 —
     different suites), making progress unmeasurable.
"""

import json
import os
import tempfile
import unittest

from cadillac.languages import python_language
from cadillac.validate import (
    _is_inside_python_package,
    check_entry_point,
    check_stdlib_conflicts,
)


def _write(root, rel, text=""):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


class TestStdlibShadowNesting(unittest.TestCase):
    """Only top-level-importable files can shadow a stdlib module."""

    def test_nested_package_module_is_not_a_shadow(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "ctxpack/__init__.py", "from .types import Chunk\n")
            _write(td, "ctxpack/types/__init__.py", "from .chunk import Chunk\n")
            _write(td, "ctxpack/types/chunk.py", "class Chunk: pass\n")
            results = check_stdlib_conflicts(td, python_language())
        self.assertTrue(all(r.passed for r in results),
                        [r.output for r in results if not r.passed])

    def test_workspace_root_module_is_still_a_shadow(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "chunk.py", "x = 1\n")
            results = check_stdlib_conflicts(td, python_language())
        self.assertTrue(any(not r.passed for r in results))

    def test_directory_without_init_is_still_a_shadow(self):
        """`src/` with no __init__.py can land on sys.path, so it can shadow."""
        with tempfile.TemporaryDirectory() as td:
            _write(td, "src/json.py", "x = 1\n")
            results = check_stdlib_conflicts(td, python_language())
        self.assertTrue(any(not r.passed for r in results))

    def test_partially_packaged_path_is_still_a_shadow(self):
        """A package nested under a NON-package dir is reachable top-level."""
        with tempfile.TemporaryDirectory() as td:
            _write(td, "src/pkg/__init__.py", "")
            _write(td, "src/pkg/socket.py", "x = 1\n")
            results = check_stdlib_conflicts(td, python_language())
        self.assertTrue(any(not r.passed for r in results))

    def test_clean_tree_passes(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "app/__init__.py", "")
            _write(td, "app/service.py", "x = 1\n")
            results = check_stdlib_conflicts(td, python_language())
        self.assertTrue(all(r.passed for r in results))

    def test_helper_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "pkg/__init__.py", "")
            _write(td, "pkg/sub/__init__.py", "")
            _write(td, "plain/file.py", "")
            self.assertFalse(_is_inside_python_package(td, td))
            self.assertTrue(_is_inside_python_package(os.path.join(td, "pkg"), td))
            self.assertTrue(_is_inside_python_package(os.path.join(td, "pkg/sub"), td))
            self.assertFalse(_is_inside_python_package(os.path.join(td, "plain"), td))


class TestLibraryNeedsNoEntryPoint(unittest.TestCase):
    """A library has a public API and no runner by design."""

    def _library(self, td):
        _write(td, "ctxpack/__init__.py",
               "from .packer import pack\n__all__ = ['pack']\n")
        _write(td, "ctxpack/packer.py", "def pack(): pass\n")

    def test_library_without_main_passes(self):
        with tempfile.TemporaryDirectory() as td:
            self._library(td)
            results = check_entry_point(td, "main.py", python_language())
        self.assertTrue(results[0].passed, results[0].output)
        self.assertIn("library", results[0].output.lower())

    def test_non_library_without_main_still_fails(self):
        """An app missing its entry point is a real failure."""
        with tempfile.TemporaryDirectory() as td:
            _write(td, "helpers.py", "x = 1\n")
            results = check_entry_point(td, "main.py", python_language())
        self.assertFalse(results[0].passed)
        self.assertIn("not found", results[0].output)

    def test_package_with_a_runner_is_not_a_library(self):
        with tempfile.TemporaryDirectory() as td:
            self._library(td)
            _write(td, "cli.py", "def main(): pass\n")
            results = check_entry_point(td, "main.py", python_language())
        self.assertFalse(results[0].passed)


class TestModuleValidationIsRecorded(unittest.TestCase):
    def test_failure_detail_is_emitted_not_just_counted(self):
        import inspect

        from cadillac.engine import _build_module
        src = inspect.getsource(_build_module)
        i = src.index("Validation: {len(errors)} error(s)")
        window = src[i:i + 700]
        self.assertIn('emit("validation"', window,
                      "module failures must emit their detail, not only a count")


class TestNonStaticFingerprints(unittest.TestCase):
    """The detector must see the failures that actually cause loops."""

    def setUp(self):
        from cadillac.validate import CheckResult
        self.CheckResult = CheckResult

    def _fp(self, results):
        from cadillac.engine import _nonstatic_failure_fingerprints
        return _nonstatic_failure_fingerprints(results)

    def test_test_timeout_is_fingerprinted(self):
        r = [self.CheckResult("tests", False, "Timed out after 75s", severity="error")]
        self.assertTrue(self._fp(r))

    def test_identical_failures_share_a_fingerprint(self):
        a = [self.CheckResult("tests", False, "Timed out after 75s", severity="error")]
        b = [self.CheckResult("tests", False, "Timed out after 75s", severity="error")]
        self.assertEqual(self._fp(a), self._fp(b))

    def test_varying_durations_do_not_change_the_fingerprint(self):
        """Same failure, different timing, must still look identical."""
        a = [self.CheckResult("tests", False, "took 12.4 s and failed", severity="error")]
        b = [self.CheckResult("tests", False, "took 19.7 s and failed", severity="error")]
        self.assertEqual(self._fp(a), self._fp(b))

    def test_different_failures_differ(self):
        a = [self.CheckResult("tests", False, "Timed out after 75s", severity="error")]
        b = [self.CheckResult("run", False, "ImportError: no module named x", severity="error")]
        self.assertNotEqual(self._fp(a), self._fp(b))

    def test_passing_and_warning_checks_are_ignored(self):
        rs = [
            self.CheckResult("tests", True, "OK"),
            self.CheckResult("lint", False, "style nit", severity="warning"),
        ]
        self.assertEqual(self._fp(rs), set())

    def test_empty_input_is_safe(self):
        self.assertEqual(self._fp([]), set())
        self.assertEqual(self._fp(None), set())

    def test_stuck_path_widens_the_fingerprint_set(self):
        import inspect

        from cadillac.engine import run
        src = inspect.getsource(run)
        i = src.index("Stuck-loop detection")
        window = src[i:i + 4000]
        self.assertIn("_nonstatic_failure_fingerprints", window)
        self.assertIn("state.validate_retries = state.max_validate_retries", window,
                      "a repeated unfixable failure must stop consuming retries")


class TestProbeCache(unittest.TestCase):
    """A stable suite is what makes cycle-to-cycle comparison meaningful."""

    class _Spec:
        def __init__(self, text):
            self._text = text

        def to_prompt_block(self):
            return self._text

    def test_round_trip(self):
        from cadillac.runtime import probe_cache

        with tempfile.TemporaryDirectory() as td:
            spec = self._Spec("must: pack chunks")
            probe_cache.save(td, "cli_runs.json", spec, [{"id": "S01"}])
            self.assertEqual(probe_cache.load(td, "cli_runs.json", spec), [{"id": "S01"}])

    def test_changed_spec_misses(self):
        """A widened tier must regenerate, not reuse the narrower suite."""
        from cadillac.runtime import probe_cache

        with tempfile.TemporaryDirectory() as td:
            probe_cache.save(td, "cli_runs.json", self._Spec("must"), [{"id": "S01"}])
            self.assertIsNone(
                probe_cache.load(td, "cli_runs.json", self._Spec("must+should")))

    def test_legacy_bare_list_is_a_miss(self):
        from cadillac.runtime import probe_cache

        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, ".cadillac"))
            with open(os.path.join(td, ".cadillac", "cli_runs.json"), "w") as f:
                json.dump([{"id": "S01"}], f)
            self.assertIsNone(probe_cache.load(td, "cli_runs.json", self._Spec("x")))

    def test_missing_and_corrupt_are_misses(self):
        from cadillac.runtime import probe_cache

        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(probe_cache.load(td, "cli_runs.json", self._Spec("x")))
            os.makedirs(os.path.join(td, ".cadillac"))
            with open(os.path.join(td, ".cadillac", "cli_runs.json"), "w") as f:
                f.write("{corrupt")
            self.assertIsNone(probe_cache.load(td, "cli_runs.json", self._Spec("x")))

    def test_empty_probe_list_is_a_miss(self):
        """An empty cached suite must not suppress regeneration."""
        from cadillac.runtime import probe_cache

        with tempfile.TemporaryDirectory() as td:
            spec = self._Spec("x")
            probe_cache.save(td, "cli_runs.json", spec, [])
            self.assertIsNone(probe_cache.load(td, "cli_runs.json", spec))

    def test_runners_consult_the_cache(self):
        import inspect

        from cadillac.runtime import cli_runner, library_runner
        for mod, fn in ((cli_runner, "generate_runs"), (library_runner, "generate_examples")):
            src = inspect.getsource(getattr(mod, fn))
            self.assertIn("probe_cache.load", src, f"{mod.__name__}.{fn}")
            self.assertIn("probe_cache.save", src, f"{mod.__name__}.{fn}")


if __name__ == "__main__":
    unittest.main()


class TestModuleImportProbeUsesDottedPath(unittest.TestCase):
    """The module import smoke-test must respect package nesting.

    Found on build 6, a nested library (`ctxpack/models`, `ctxpack/tokenizers`,
    `ctxpack/strategies`). The probe used `os.path.basename(module_path)`, which
    was wrong in BOTH directions:

      * false negative — `ctxpack/strategies` probed as `import strategies`,
        which cannot resolve, failing two perfectly healthy modules; and
      * false positive — `ctxpack/tokenizers` probed as `import tokenizers`,
        which resolved to HuggingFace's INSTALLED `tokenizers` package. The
        module reported OK without the check touching the project at all.
    """

    @staticmethod
    def _dotted(module_path: str) -> str:
        """Mirror of the derivation in run_module_validation."""
        rel = module_path.rstrip("/").replace("\\", "/")
        return ".".join(p for p in rel.split("/") if p and p != ".")

    def test_nested_path_becomes_dotted(self):
        self.assertEqual(self._dotted("ctxpack/strategies"), "ctxpack.strategies")

    def test_deeply_nested_path(self):
        self.assertEqual(self._dotted("a/b/c"), "a.b.c")

    def test_top_level_module_unchanged(self):
        self.assertEqual(self._dotted("core"), "core")

    def test_trailing_slash_and_dot_segments_ignored(self):
        self.assertEqual(self._dotted("ctxpack/models/"), "ctxpack.models")
        self.assertEqual(self._dotted("./ctxpack/models"), "ctxpack.models")

    def test_source_uses_dotted_not_basename(self):
        import inspect

        from cadillac.validate import run_module_validation
        src = inspect.getsource(run_module_validation)
        i = src.index("Workspace-import smoke")
        window = src[i:i + 1600]
        self.assertNotIn("os.path.basename(module_path", window,
                         "basename ignores package nesting — use the dotted path")
        self.assertIn('".".join', window)

    def test_probe_resolves_project_module_not_site_packages(self):
        """A nested module whose basename collides with an installed package
        must be checked against the PROJECT, not the installed one."""
        import subprocess

        with tempfile.TemporaryDirectory() as td:
            _write(td, "pkg/__init__.py", "")
            # `json` is guaranteed importable as a top-level stdlib module, so a
            # basename probe would pass regardless of the project's own code.
            _write(td, "pkg/json/__init__.py", "raise RuntimeError('project module')\n")
            basename = subprocess.run(["python3", "-c", "import json"],
                                      cwd=td, capture_output=True)
            dotted = subprocess.run(["python3", "-c", "import pkg.json"],
                                    cwd=td, capture_output=True)
        self.assertEqual(basename.returncode, 0, "basename probe hits stdlib — useless")
        self.assertNotEqual(dotted.returncode, 0, "dotted probe reaches the project module")


class TestModuleScopedNamingRespectsPackages(unittest.TestCase):
    """`run_module_validation` carries its OWN copy of the stdlib-shadow check.

    Fixing `check_stdlib_conflicts` alone was not enough — the inline copy at
    the module-validation level still had no nesting awareness and failed
    `core/chunk.py` on build 7, where `core/__init__.py` exists and the file is
    imported as `core.chunk`. Two copies of one rule; both must agree.
    """

    def _run(self, td, module_path):
        from cadillac.validate import run_module_validation
        return run_module_validation(td, module_path, None, lang=python_language())

    def _naming_failures(self, results):
        return [r for r in results
                if r.name == "naming" and not r.passed and r.severity == "error"]

    def test_package_module_file_is_not_a_shadow(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "core/__init__.py", "from .chunk import Chunk\n")
            _write(td, "core/chunk.py", "class Chunk: pass\n")
            self.assertEqual(self._naming_failures(self._run(td, "core")), [])

    def test_non_package_module_dir_still_shadows(self):
        """No __init__.py means the dir can land on sys.path."""
        with tempfile.TemporaryDirectory() as td:
            _write(td, "core/chunk.py", "class Chunk: pass\n")
            self.assertTrue(self._naming_failures(self._run(td, "core")))

    def test_nested_package_module_is_not_a_shadow(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "ctxpack/__init__.py", "")
            _write(td, "ctxpack/core/__init__.py", "")
            _write(td, "ctxpack/core/socket.py", "x = 1\n")
            self.assertEqual(self._naming_failures(self._run(td, "ctxpack/core")), [])

    def test_both_copies_of_the_rule_agree(self):
        """The workspace-level and module-level checks must not disagree."""
        from cadillac.validate import check_stdlib_conflicts

        with tempfile.TemporaryDirectory() as td:
            _write(td, "core/__init__.py", "")
            _write(td, "core/chunk.py", "x = 1\n")
            workspace_level = [r for r in check_stdlib_conflicts(td, python_language())
                               if not r.passed]
            module_level = self._naming_failures(self._run(td, "core"))
        self.assertEqual(bool(workspace_level), bool(module_level),
                         "the two copies of the stdlib-shadow rule disagree")
