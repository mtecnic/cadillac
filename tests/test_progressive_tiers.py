"""Tests for the progressive-tier scope: Spec.subset() and tier orchestration.

Spec.subset is pure; tested directly. The engine-level orchestration is
tested by exercising the build-tier helper through a unit hook.
"""

from __future__ import annotations

import unittest

from cadillac.phases import PhaseState
from cadillac.spec import Spec, Story


class TestSpecSubset(unittest.TestCase):
    def setUp(self):
        self.spec = Spec(task="t", stories=[
            Story(id="S01", title="signup", acceptance=("a",), priority="must"),
            Story(id="S02", title="login", acceptance=("a",), priority="must"),
            Story(id="S03", title="logout", acceptance=("a",), priority="should"),
            Story(id="S04", title="search", acceptance=("a",), priority="should"),
            Story(id="S05", title="archive", acceptance=("a",), priority="could"),
        ])

    def test_must_only(self):
        sub = self.spec.subset(("must",))
        self.assertEqual({s.id for s in sub.stories}, {"S01", "S02"})

    def test_must_and_should(self):
        sub = self.spec.subset(("must", "should"))
        self.assertEqual({s.id for s in sub.stories}, {"S01", "S02", "S03", "S04"})

    def test_full(self):
        sub = self.spec.subset(("must", "should", "could"))
        self.assertEqual(len(sub.stories), 5)

    def test_subset_preserves_task_and_model(self):
        s = Spec(task="x", model="abc", stories=[
            Story(id="S01", title="t", acceptance=("a",), priority="must"),
        ])
        sub = s.subset(("must",))
        self.assertEqual(sub.task, "x")
        self.assertEqual(sub.model, "abc")


class TestPhaseStateTierFields(unittest.TestCase):
    def test_defaults(self):
        s = PhaseState()
        self.assertEqual(s.current_tier, "all")
        self.assertEqual(s.stuck_fingerprints, [])
        self.assertEqual(s.surgical_fixes_attempted, set())
        self.assertFalse(s.completeness_critic_done)
        self.assertFalse(s.runtime_verify_done)

    def test_per_instance_isolation(self):
        """Default factories must produce fresh containers per instance —
        a previous bug class had `field(default=[])` leaking state across
        builds; this test pins the guard."""
        a = PhaseState()
        b = PhaseState()
        a.stuck_fingerprints.append("x")
        a.surgical_fixes_attempted.add("y")
        self.assertEqual(b.stuck_fingerprints, [])
        self.assertEqual(b.surgical_fixes_attempted, set())


class TestTierBuilder(unittest.TestCase):
    """Mirror the inline _build_tiers logic in engine.run() so test
    coverage doesn't depend on running the full pipeline."""

    def _build_tiers(self, spec: Spec, full: bool):
        has_must = any(s.priority == "must" for s in spec.stories)
        has_should = any(s.priority == "should" for s in spec.stories)
        has_could = any(s.priority == "could" for s in spec.stories)
        out = []
        if not spec.stories:
            return out
        if has_must:
            out.append(("must", spec.subset(("must",))))
        if has_should:
            out.append(("must+should", spec.subset(("must", "should"))))
        elif not has_must:
            out.append(("all", spec))
        if has_could and full:
            out.append(("must+should+could", spec))
        return out

    def test_full_three_tier(self):
        spec = Spec(task="t", stories=[
            Story(id="S01", title="a", acceptance=("a",), priority="must"),
            Story(id="S02", title="b", acceptance=("a",), priority="should"),
            Story(id="S03", title="c", acceptance=("a",), priority="could"),
        ])
        tiers = self._build_tiers(spec, full=True)
        self.assertEqual(len(tiers), 3)
        self.assertEqual(tiers[0][0], "must")
        self.assertEqual(tiers[1][0], "must+should")
        self.assertEqual(tiers[2][0], "must+should+could")

    def test_default_skips_could(self):
        spec = Spec(task="t", stories=[
            Story(id="S01", title="a", acceptance=("a",), priority="must"),
            Story(id="S02", title="b", acceptance=("a",), priority="should"),
            Story(id="S03", title="c", acceptance=("a",), priority="could"),
        ])
        tiers = self._build_tiers(spec, full=False)
        self.assertEqual(len(tiers), 2)
        self.assertEqual([t[0] for t in tiers], ["must", "must+should"])

    def test_must_only_one_tier(self):
        spec = Spec(task="t", stories=[
            Story(id="S01", title="a", acceptance=("a",), priority="must"),
        ])
        tiers = self._build_tiers(spec, full=True)
        self.assertEqual(len(tiers), 1)
        self.assertEqual(tiers[0][0], "must")

    def test_only_could_falls_back_to_single_all_tier(self):
        """A spec with only could-priority stories shouldn't refuse to
        build — it falls back to a single 'all' tier so the build still
        ships, just without the progressive split."""
        spec = Spec(task="t", stories=[
            Story(id="S01", title="a", acceptance=("a",), priority="could"),
        ])
        tiers = self._build_tiers(spec, full=False)
        self.assertEqual(len(tiers), 1)
        self.assertEqual(tiers[0][0], "all")
        self.assertEqual(len(tiers[0][1].stories), 1)

    def test_empty_spec_no_tiers(self):
        spec = Spec(task="t", stories=[])
        self.assertEqual(self._build_tiers(spec, full=True), [])


if __name__ == "__main__":
    unittest.main()
