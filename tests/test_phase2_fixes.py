"""Tests pinning the two real findings from the Phase 2 limit-tests:

1. add_dep must bootstrap package.json when missing — multi-language
   workspaces (Python ML + TS frontend) couldn't add JS deps because the
   primary language scaffolding skipped Node setup. Found while building
   ImageClassifier (PyTorch trainer + FastAPI server + React UI).

2. check_functional_smoke must skip well-known runtime/artifact directories
   (checkpoints/, runs/, logs/, models/, data/) — they're correctly empty
   until the program runs. Found while building ImageClassifier where
   Cadillac kept retrying on a false-positive about an empty checkpoints/
   directory.
"""

import json
import os
import tempfile
import unittest

from cadillac.languages import python_language
from cadillac.manifest import FileManifest
from cadillac.tools import ToolExecutor


class TestAddDepBootstrap(unittest.TestCase):
    def _executor(self, td: str) -> ToolExecutor:
        return ToolExecutor(td, FileManifest())

    def test_creates_package_json_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            ex = self._executor(td)
            self.assertFalse(os.path.exists(os.path.join(td, "package.json")))
            r = ex.add_dep("react", "^18", dev=False)
            self.assertEqual(r.get("status"), "ok",
                             f"expected ok, got {r}")
            pkg_path = os.path.join(td, "package.json")
            self.assertTrue(os.path.exists(pkg_path))
            with open(pkg_path) as f:
                pkg = json.load(f)
            # Bootstrap shape — minimal but complete
            self.assertEqual(pkg["name"], "project")
            self.assertEqual(pkg["private"], True)
            self.assertIn("react", pkg["dependencies"])
            # devDependencies + scripts must exist (even empty) so subsequent
            # add_dep calls don't re-bootstrap
            self.assertIn("devDependencies", pkg)
            self.assertIn("scripts", pkg)

    def test_uses_existing_package_json(self):
        with tempfile.TemporaryDirectory() as td:
            existing = {
                "name": "myapp", "version": "2.0.0", "private": False,
                "dependencies": {"existing-dep": "^1"},
                "devDependencies": {},
                "scripts": {"build": "vite build"},
            }
            with open(os.path.join(td, "package.json"), "w") as f:
                json.dump(existing, f)
            ex = self._executor(td)
            r = ex.add_dep("react", "^18", dev=False)
            self.assertEqual(r.get("status"), "ok")
            with open(os.path.join(td, "package.json")) as f:
                pkg = json.load(f)
            # Existing fields preserved
            self.assertEqual(pkg["name"], "myapp")
            self.assertEqual(pkg["version"], "2.0.0")
            self.assertEqual(pkg["private"], False)
            self.assertEqual(pkg["scripts"]["build"], "vite build")
            self.assertIn("existing-dep", pkg["dependencies"])
            # New dep added
            self.assertIn("react", pkg["dependencies"])

    def test_dev_dep_bootstrap(self):
        with tempfile.TemporaryDirectory() as td:
            ex = self._executor(td)
            r = ex.add_dep("vitest", "^1", dev=True)
            self.assertEqual(r.get("status"), "ok")
            with open(os.path.join(td, "package.json")) as f:
                pkg = json.load(f)
            self.assertIn("vitest", pkg["devDependencies"])
            self.assertNotIn("vitest", pkg["dependencies"])


class TestRuntimeDirsNotFlaggedAsEmptyModule(unittest.TestCase):
    """Phase 2 limit-test regression — checkpoints/ kept being flagged."""

    def _make_workspace(self, td: str) -> None:
        # A typical PyTorch project: src has code, checkpoints/runs/logs are
        # empty until train.py runs.
        os.makedirs(os.path.join(td, "src/ml"))
        with open(os.path.join(td, "src/__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(td, "src/ml/__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(td, "src/ml/model.py"), "w") as f:
            f.write("def build_model(): return None\n")
        with open(os.path.join(td, "main.py"), "w") as f:
            f.write("from src.ml.model import build_model\n"
                    "if __name__ == '__main__': build_model()\n")
        # Runtime / artifact dirs that exist but are empty
        for d in ("checkpoints", "runs", "logs", "models", "data"):
            os.makedirs(os.path.join(td, d))

    def test_checkpoints_runs_logs_not_flagged(self):
        from cadillac.validate import check_functional_smoke
        with tempfile.TemporaryDirectory() as td:
            self._make_workspace(td)
            results = check_functional_smoke(td, "main.py", python_language())
            failures = [r for r in results if not r.passed and r.severity == "error"]
            empty_dir_failures = [
                r for r in failures
                if "Empty module directories" in r.output
                or "no .py files" in r.output
            ]
            self.assertEqual(
                empty_dir_failures, [],
                f"runtime/artifact dirs were wrongly flagged: "
                f"{[f.output for f in empty_dir_failures]}",
            )

    def test_real_unfilled_module_still_flagged(self):
        """Sanity: an actual planned-but-never-built module dir SHOULD still fire.
        Without this we'd be silencing the real signal."""
        from cadillac.validate import check_functional_smoke
        with tempfile.TemporaryDirectory() as td:
            self._make_workspace(td)
            # This one is NOT in the safelist — it's genuinely an empty module.
            os.makedirs(os.path.join(td, "auth"))
            results = check_functional_smoke(td, "main.py", python_language())
            failures = [r for r in results if not r.passed and r.severity == "error"]
            empty_dir_failures = [
                r for r in failures
                if "auth" in r.output and "Empty module" in r.output
            ]
            self.assertTrue(
                empty_dir_failures,
                "an actual empty module dir should still fire — without this, "
                "we'd be silencing the real signal",
            )


if __name__ == "__main__":
    unittest.main()
