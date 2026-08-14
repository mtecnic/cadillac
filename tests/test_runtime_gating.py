"""Tests for the two defects build 10 exposed.

Build 10 finished `COMPLETE | all validations pass | PACKAGE Done` and shipped a
library that could not be imported by its own name:

    $ import ctxpack  ->  ModuleNotFoundError
    [RUNTIME] strategy=library probes_run=16 failures=16   (twice)

Two independent failures produced that:

  1. UNRESOLVED RUNTIME FAILURES WERE DISCARDED. When `retreat_to_build()`
     returned False the code logged "advisory" and fell through to the success
     path. RUNTIME is the only layer that drives the artifact the way a user
     would, and its verdict never reached the outcome.
  2. THE LIBRARY WAS LAID OUT UNIMPORTABLE. The planner put the package
     contents at the workspace root with a bare root `__init__.py`, so the
     package name never existed. Every static check passed because each one
     exercises the code IN PLACE rather than as an installed package —
     `functional` in particular returned a vacuous "No packages to smoke test".
"""

import json
import os
import tempfile
import unittest

from cadillac.engine import (
    RUNTIME_FAILURES_FILE,
    _clear_runtime_failures,
    _record_runtime_failures,
    read_runtime_failures,
)
from cadillac.languages import python_language
from cadillac.validate import check_functional_smoke


class _Probe:
    def __init__(self, story_id="S01", priority="must"):
        self.story_id = story_id
        self.priority = priority


class _Failure:
    def __init__(self, story_id="S01", kind="import_error", detail="boom"):
        self.probe = _Probe(story_id)
        self.failure_kind = kind
        self.detail = detail


class TestRuntimeFailureRecord(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def test_clean_workspace_reports_none(self):
        self.assertIsNone(read_runtime_failures(self.ws))

    def test_recorded_failures_are_readable(self):
        _record_runtime_failures(self.ws, "library", [_Failure(), _Failure("S02")])
        rt = read_runtime_failures(self.ws)
        self.assertIsNotNone(rt)
        self.assertEqual(rt["count"], 2)
        self.assertEqual(rt["strategy"], "library")

    def test_record_is_written_atomically(self):
        _record_runtime_failures(self.ws, "cli", [_Failure()])
        leftovers = [f for f in os.listdir(os.path.join(self.ws, ".cadillac"))
                     if ".tmp." in f]
        self.assertEqual(leftovers, [])

    def test_clear_removes_the_marker(self):
        _record_runtime_failures(self.ws, "cli", [_Failure()])
        _clear_runtime_failures(self.ws)
        self.assertIsNone(read_runtime_failures(self.ws))

    def test_clear_on_clean_workspace_is_safe(self):
        _clear_runtime_failures(self.ws)  # must not raise

    def test_zero_failures_is_not_a_failure_record(self):
        _record_runtime_failures(self.ws, "cli", [])
        self.assertIsNone(read_runtime_failures(self.ws))

    def test_corrupt_marker_reads_as_none(self):
        os.makedirs(os.path.join(self.ws, ".cadillac"), exist_ok=True)
        with open(os.path.join(self.ws, ".cadillac", RUNTIME_FAILURES_FILE), "w") as f:
            f.write("{corrupt")
        self.assertIsNone(read_runtime_failures(self.ws))

    def test_detail_is_bounded(self):
        _record_runtime_failures(self.ws, "cli", [_Failure(detail="x" * 5000)])
        rt = read_runtime_failures(self.ws)
        self.assertLessEqual(len(rt["failures"][0]["detail"]), 300)

    def test_recording_never_raises_on_a_bad_path(self):
        _record_runtime_failures("/proc/nonexistent-xyz", "cli", [_Failure()])


class TestBuildDoesNotClaimSuccessWithFailingProbes(unittest.TestCase):
    """The reporting half: static-green must not read as success when the
    artifact demonstrably does not behave as specified."""

    def test_success_path_consults_the_runtime_record(self):
        import inspect

        from cadillac.engine import build
        src = inspect.getsource(build)
        self.assertIn("read_runtime_failures", src,
                      "build() must consult unresolved runtime failures")
        # NOTE: match the EMIT, not the bare phrase — the explanatory comment
        # above the check also contains "All validations pass!".
        emit_call = 'msg="[AUTO] All validations pass!"'
        self.assertLess(src.index("read_runtime_failures"), src.index(emit_call),
                        "the check must run BEFORE success is reported")

    def test_no_retreat_blocked_path_discards_its_findings(self):
        """RUNTIME and CRITIC both had the identical
        "retreat blocked -> log advisory -> fall through to success" discard.
        Fixing only the one that build 10 exercised would have left the other."""
        import inspect

        from cadillac.engine import run
        src = inspect.getsource(run)
        for phase in ("[RUNTIME] retreat blocked", "[CRITIC] retreat blocked"):
            i = src.index(f'"{phase}')
            window = src[i:i + 1200]
            self.assertIn("_record_runtime_failures", window,
                          f"{phase} discards its findings")
        self.assertNotIn("logging as advisory", src,
                         "unresolved findings must not be downgraded to a log line")


class TestLibraryLayoutIsEnforced(unittest.TestCase):
    """Prompt guidance alone has not been reliable, so the layout is checked."""

    def _ws(self, files):
        td = tempfile.mkdtemp()
        for rel, body in files.items():
            path = os.path.join(td, rel)
            os.makedirs(os.path.dirname(path) or td, exist_ok=True)
            with open(path, "w") as f:
                f.write(body)
        return td

    def test_root_init_is_a_failure(self):
        ws = self._ws({
            "__init__.py": "from .models import Chunk\n",
            "models/__init__.py": "",
            "models/chunk.py": "class Chunk: pass\n",
        })
        results = check_functional_smoke(ws, "__init__.py", python_language())
        self.assertTrue(any(not r.passed for r in results))
        self.assertIn("imported by its own name",
                      " ".join(str(r.output) for r in results))

    def test_correct_library_layout_passes(self):
        ws = self._ws({
            "mylib/__init__.py": "from .models import Chunk\n__all__ = ['Chunk']\n",
            "mylib/models/__init__.py": "from .chunk import Chunk\n",
            "mylib/models/chunk.py": "class Chunk: pass\n",
        })
        results = check_functional_smoke(ws, "mylib/__init__.py", python_language())
        self.assertTrue(all(r.passed for r in results),
                        [r.output for r in results if not r.passed])

    def test_ordinary_app_is_unaffected(self):
        ws = self._ws({
            "main.py": "def main(): pass\n",
            "core/__init__.py": "",
            "core/service.py": "def go(): pass\n",
        })
        results = check_functional_smoke(ws, "main.py", python_language())
        self.assertTrue(all(r.passed for r in results),
                        [r.output for r in results if not r.passed])

    def test_non_python_is_unaffected(self):
        from cadillac.languages import react_language

        ws = self._ws({"__init__.py": "", "src/index.ts": "export const x = 1;\n"})
        results = check_functional_smoke(ws, "src/index.ts", react_language())
        self.assertFalse(any("imported by its own name" in str(r.output)
                             for r in results))


class TestPlanPromptStatesLibraryLayout(unittest.TestCase):
    def test_modular_plan_prompt_has_the_library_rule(self):
        from cadillac.prompts import _MODULAR_MANIFEST_TEMPLATE
        self.assertIn("LIBRARY LAYOUT", _MODULAR_MANIFEST_TEMPLATE)
        self.assertIn("ModuleNotFoundError", _MODULAR_MANIFEST_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
