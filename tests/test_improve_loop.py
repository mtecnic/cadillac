"""Tests for the cadillac improve self-improvement cycle.

The LLM-driven stages (audit, correlate, propose) are tested via inputs
and outputs at the boundary, with fake chat responses. The applier is
tested with real git commands against a temp repo. Matrix runs and the
full cycle are not tested end-to-end here — that requires the LAN LLM
and 30 minutes per iteration; covered by the smoke-test docs.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cadillac.improve.scoring import (
    IterationScore, TaskOutcome, is_improvement, is_saturated,
)
from cadillac.improve.log import ImproveLog


class TestScoring(unittest.TestCase):
    def test_task_outcome_score_basic(self):
        # All checks pass, no retries, 60s — score ≈ 0.94
        o = TaskOutcome(
            task_id="t", passed_checks=5, total_checks=5,
            retry_rounds=0, elapsed_s=60,
        )
        self.assertAlmostEqual(o.score(), 1.0 - 0.001 * 60, places=4)

    def test_task_outcome_crashed_floor(self):
        o = TaskOutcome(task_id="t", passed_checks=0, total_checks=5,
                        retry_rounds=10, elapsed_s=900, crashed=True)
        self.assertEqual(o.score(), -1.0)

    def test_aggregate_score(self):
        outcomes = [
            TaskOutcome(task_id="a", passed_checks=5, total_checks=5,
                        retry_rounds=0, elapsed_s=10),
            TaskOutcome(task_id="b", passed_checks=3, total_checks=5,
                        retry_rounds=2, elapsed_s=30),
        ]
        s = IterationScore(iteration=1, outcomes=outcomes)
        # First task: 1.0 - 0.001*10 = 0.99
        # Second: 0.6 - 0.02 - 0.03 = 0.55
        # Mean ≈ 0.77
        self.assertGreater(s.aggregate, 0.7)
        self.assertLess(s.aggregate, 0.8)

    def test_is_improvement_first_iter(self):
        s = IterationScore(iteration=1, outcomes=[])
        ok, reason = is_improvement(None, s)
        self.assertTrue(ok)
        self.assertIn("baseline", reason)

    def test_is_improvement_aggregate_up(self):
        prev = IterationScore(iteration=1, outcomes=[
            TaskOutcome(task_id="a", passed_checks=3, total_checks=5,
                        retry_rounds=0, elapsed_s=10),
        ])
        curr = IterationScore(iteration=2, outcomes=[
            TaskOutcome(task_id="a", passed_checks=5, total_checks=5,
                        retry_rounds=0, elapsed_s=10),
        ])
        ok, _ = is_improvement(prev, curr)
        self.assertTrue(ok)

    def test_is_improvement_retries_dropped(self):
        prev = IterationScore(iteration=1, outcomes=[
            TaskOutcome(task_id="a", passed_checks=5, total_checks=5,
                        retry_rounds=10, elapsed_s=10),
        ])
        curr = IterationScore(iteration=2, outcomes=[
            TaskOutcome(task_id="a", passed_checks=5, total_checks=5,
                        retry_rounds=2, elapsed_s=10),
        ])
        ok, _ = is_improvement(prev, curr)
        self.assertTrue(ok)

    def test_is_improvement_regression(self):
        prev = IterationScore(iteration=1, outcomes=[
            TaskOutcome(task_id="a", passed_checks=5, total_checks=5,
                        retry_rounds=0, elapsed_s=10),
        ])
        curr = IterationScore(iteration=2, outcomes=[
            TaskOutcome(task_id="a", passed_checks=3, total_checks=5,
                        retry_rounds=0, elapsed_s=10),
        ])
        ok, _ = is_improvement(prev, curr)
        self.assertFalse(ok)

    def test_is_saturated(self):
        outcomes = [
            TaskOutcome(task_id=f"t{i}", passed_checks=5, total_checks=5,
                        retry_rounds=0, elapsed_s=10)
            for i in range(8)
        ]
        s = IterationScore(iteration=1, outcomes=outcomes)
        self.assertTrue(is_saturated(s))


class TestLog(unittest.TestCase):
    def test_append_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            log = ImproveLog(os.path.join(td, "log.jsonl"))
            log.append({"iteration": 1, "aggregate": 0.5})
            log.append({"iteration": 2, "aggregate": 0.6})
            records = log.read()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["iteration"], 1)
            self.assertEqual(records[1]["aggregate"], 0.6)
            # Each record gets a ts injected
            for r in records:
                self.assertIn("ts", r)

    def test_skips_malformed_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "log.jsonl")
            with open(path, "w") as f:
                f.write('{"iteration": 1}\n')
                f.write("garbage line not json\n")
                f.write('{"iteration": 2}\n')
            log = ImproveLog(path)
            self.assertEqual(len(log.read()), 2)


class TestAuditFiltersImprovePath(unittest.TestCase):
    """The audit must NEVER return findings about cadillac/improve/* —
    that's the loop running the audit. Otherwise we get confirmation-bias
    self-modification."""

    def test_filter_strips_improve_paths(self):
        from cadillac.improve.audit import _filter_self_modifications
        raw = [
            {"file": "cadillac/engine.py", "weakness_id": "x"},
            {"file": "cadillac/improve/audit.py", "weakness_id": "y"},
            {"file": "cadillac/improve/cli.py", "weakness_id": "z"},
            {"file": "cadillac/validate.py", "weakness_id": "w"},
        ]
        filtered = _filter_self_modifications(raw)
        ids = {f["weakness_id"] for f in filtered}
        self.assertEqual(ids, {"x", "w"},
                         "improve/* findings must be stripped")


class TestProposerSplit(unittest.TestCase):
    """The proposer parses LLM output for PATCH and TEST sections."""

    def test_split_response_basic(self):
        from cadillac.improve.proposer import _split_response
        raw = """Some preamble.
═══ PATCH ═══
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
- old
+ new
═══ TEST ═══
import unittest
class T(unittest.TestCase):
    def test_x(self): self.assertTrue(True)
"""
        patch, test = _split_response(raw)
        self.assertIn("--- a/foo.py", patch)
        self.assertIn("+ new", patch)
        self.assertIn("import unittest", test)
        self.assertIn("test_x", test)

    def test_split_handles_fenced_sections(self):
        from cadillac.improve.proposer import _split_response
        raw = """═══ PATCH ═══
```diff
--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-old
+new
```
═══ TEST ═══
```python
def test_x(): pass
```
"""
        patch, test = _split_response(raw)
        # Fences should be stripped
        self.assertNotIn("```", patch)
        self.assertNotIn("```", test)
        self.assertIn("--- a/x.py", patch)

    def test_missing_markers_returns_empty(self):
        from cadillac.improve.proposer import _split_response
        raw = "Here is a fix:\n```python\ndef x(): pass\n```"
        patch, test = _split_response(raw)
        self.assertEqual(patch, "")
        self.assertEqual(test, "")

    def test_self_modification_blocked(self):
        from cadillac.improve.proposer import _is_self_modifying
        good = "--- a/cadillac/engine.py\n+++ b/cadillac/engine.py\n"
        bad1 = "--- a/cadillac/improve/cli.py\n+++ b/cadillac/improve/cli.py\n"
        bad2 = "diff --git a/cadillac/improve/audit.py b/cadillac/improve/audit.py\n"
        self.assertFalse(_is_self_modifying(good))
        self.assertTrue(_is_self_modifying(bad1))
        self.assertTrue(_is_self_modifying(bad2))


class TestApplierSafeguards(unittest.TestCase):
    """The applier must refuse to apply on a dirty tree and revert cleanly."""

    def _make_repo(self, td: str) -> str:
        repo = os.path.join(td, "repo")
        os.makedirs(repo)
        os.makedirs(os.path.join(repo, "cadillac"))
        os.makedirs(os.path.join(repo, "cadillac/tests"))
        # Minimal package init so unittest discovery doesn't crash
        with open(os.path.join(repo, "cadillac/__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(repo, "cadillac/tests/__init__.py"), "w") as f:
            f.write("")
        # Real source file we can patch
        with open(os.path.join(repo, "cadillac/target.py"), "w") as f:
            f.write("def add(a, b):\n    return a + b\n")
        # Dummy passing test so unittest discovery returns 0
        with open(os.path.join(repo, "cadillac/tests/test_target.py"), "w") as f:
            f.write(
                "import unittest\n"
                "from cadillac.target import add\n"
                "class T(unittest.TestCase):\n"
                "    def test_add(self): self.assertEqual(add(1,2), 3)\n"
            )
        # Init git
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t",
                        "-c", "user.name=t", "add", "."],
                       cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-m", "init"],
                       cwd=repo, check=True, capture_output=True)
        return repo

    def test_refuses_dirty_tree(self):
        from cadillac.improve.applier import apply_proposal
        from cadillac.improve.proposer import Proposal
        with tempfile.TemporaryDirectory() as td:
            repo = self._make_repo(td)
            # Create dirty state
            with open(os.path.join(repo, "dirty.txt"), "w") as f:
                f.write("uncommitted")
            subprocess.run(["git", "add", "dirty.txt"],
                           cwd=repo, capture_output=True)
            proposal = Proposal(
                weakness_id="x", patch="", test_path="tests/test_x.py",
                test_content="", target_file="cadillac/target.py",
            )
            result = apply_proposal(proposal, repo, lambda *a, **kw: None)
            self.assertFalse(result.success)
            self.assertIn("dirty", result.reason.lower())


if __name__ == "__main__":
    unittest.main()
