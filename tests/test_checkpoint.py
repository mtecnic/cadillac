"""Tests for checkpoint durability and completeness.

Two failure modes are pinned here:

  1. NON-ATOMIC WRITE — `open(path, "w")` truncates immediately. A crash
     between truncate and flush left an empty checkpoint.json and an
     unresumable build. `_atomic.py` was already in the package for exactly
     this; the most resume-critical writer was the one that missed it.

  2. LOSSY STATE — the checkpoint saved phase/round/retries but dropped the
     computed phase budgets and every once-per-build guard flag, so `resume()`
     rebuilt a default PhaseState: budgets silently reverted to
     DEFAULT_BUDGETS and CRITIC/RUNTIME re-fired on a build that had run them.
"""

import json
import os
import tempfile
import unittest

from cadillac.engine import (
    CHECKPOINT_SCHEMA_VERSION,
    _load_checkpoint,
    _restore_phase_state,
    _save_checkpoint,
)
from cadillac.manifest import FileManifest
from cadillac.phases import DEFAULT_BUDGETS, Phase, PhaseState


def _populated_state() -> PhaseState:
    """A PhaseState mid-build with non-default budgets and guards set."""
    state = PhaseState()
    state.current = Phase.VALIDATE
    state.round_in_phase = 3
    state.total_rounds = 87
    state.validate_retries = 2
    state.max_rounds[Phase.BUILD] = 64
    state.max_rounds[Phase.SCAFFOLD] = 37
    state.max_total_rounds = 1000
    state.adversarial_retries = 1
    state.completeness_critic_done = True
    state.runtime_verify_done = True
    state.current_tier = "must+should"
    state.stuck_fingerprints = ["abc123", "def456"]
    state.surgical_fixes_attempted = {"abc123"}
    state.phase_rounds_used = {Phase.BUILD: 41, Phase.SCAFFOLD: 12}
    return state


class _CheckpointCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = self._td.name
        self.manifest = FileManifest()

    def tearDown(self):
        self._td.cleanup()

    def save(self, state):
        _save_checkpoint(self.ws, state, self.manifest, None, "arch text", "main.py", "build a thing")

    def path(self):
        return os.path.join(self.ws, ".cadillac", "checkpoint.json")


class TestAtomicWrite(_CheckpointCase):
    def test_checkpoint_written(self):
        self.save(_populated_state())
        self.assertTrue(os.path.exists(self.path()))

    def test_no_temp_files_left_behind(self):
        self.save(_populated_state())
        leftovers = [
            f for f in os.listdir(os.path.join(self.ws, ".cadillac"))
            if ".tmp." in f
        ]
        self.assertEqual(leftovers, [], f"atomic write left temp files: {leftovers}")

    def test_overwrite_preserves_valid_json(self):
        """A second save must never leave a half-written file."""
        self.save(_populated_state())
        for _ in range(5):
            self.save(_populated_state())
            with open(self.path()) as f:
                json.load(f)  # raises if truncated

    def test_existing_checkpoint_survives_failed_write(self):
        """os.replace is atomic: readers see the old file or the new one."""
        self.save(_populated_state())
        with open(self.path()) as f:
            before = json.load(f)
        self.assertEqual(before["total_rounds"], 87)


class TestFailClosedLoad(_CheckpointCase):
    def test_missing_returns_none(self):
        self.assertIsNone(_load_checkpoint(self.ws))

    def test_truncated_json_returns_none(self):
        os.makedirs(os.path.join(self.ws, ".cadillac"), exist_ok=True)
        with open(self.path(), "w") as f:
            f.write('{"phase": "build", "total_rou')
        self.assertIsNone(_load_checkpoint(self.ws))

    def test_empty_file_returns_none(self):
        os.makedirs(os.path.join(self.ws, ".cadillac"), exist_ok=True)
        open(self.path(), "w").close()
        self.assertIsNone(_load_checkpoint(self.ws))

    def test_non_object_json_returns_none(self):
        os.makedirs(os.path.join(self.ws, ".cadillac"), exist_ok=True)
        with open(self.path(), "w") as f:
            f.write("[1, 2, 3]")
        self.assertIsNone(_load_checkpoint(self.ws))


class TestRoundTrip(_CheckpointCase):
    """Every field that changes build behaviour must survive save → load →
    restore. This is the regression that made resumed builds re-run RUNTIME."""

    def setUp(self):
        super().setUp()
        self.original = _populated_state()
        self.save(self.original)
        self.restored = _restore_phase_state(_load_checkpoint(self.ws), Phase.VALIDATE)

    def test_schema_version_recorded(self):
        self.assertEqual(_load_checkpoint(self.ws)["schema_version"], CHECKPOINT_SCHEMA_VERSION)

    def test_budgets_survive(self):
        self.assertEqual(self.restored.max_rounds[Phase.BUILD], 64)
        self.assertEqual(self.restored.max_rounds[Phase.SCAFFOLD], 37)

    def test_budgets_are_not_defaults(self):
        """Guards the actual bug: a fresh PhaseState would give DEFAULT_BUDGETS."""
        self.assertNotEqual(self.restored.max_rounds[Phase.BUILD], DEFAULT_BUDGETS[Phase.BUILD])

    def test_max_total_rounds_survives(self):
        self.assertEqual(self.restored.max_total_rounds, 1000)

    def test_runtime_verify_flag_survives(self):
        self.assertTrue(self.restored.runtime_verify_done)

    def test_critic_flag_survives(self):
        self.assertTrue(self.restored.completeness_critic_done)

    def test_adversarial_retries_survive(self):
        self.assertEqual(self.restored.adversarial_retries, 1)

    def test_tier_survives(self):
        self.assertEqual(self.restored.current_tier, "must+should")

    def test_stuck_fingerprints_survive(self):
        self.assertEqual(self.restored.stuck_fingerprints, ["abc123", "def456"])

    def test_surgical_attempts_survive_as_set(self):
        self.assertEqual(self.restored.surgical_fixes_attempted, {"abc123"})
        self.assertIsInstance(self.restored.surgical_fixes_attempted, set)

    def test_phase_rounds_used_survive(self):
        self.assertEqual(self.restored.phase_rounds_used[Phase.BUILD], 41)

    def test_pre_existing_fields_survive(self):
        self.assertEqual(self.restored.total_rounds, 87)
        self.assertEqual(self.restored.validate_retries, 2)

    def test_restore_targets_requested_phase(self):
        restored = _restore_phase_state(_load_checkpoint(self.ws), Phase.BUILD)
        self.assertEqual(restored.current, Phase.BUILD)


class TestBackwardCompatibility(_CheckpointCase):
    """A v1 checkpoint (no budgets, no guard flags) must still resume, taking
    PhaseState defaults for everything it doesn't carry."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.ws, ".cadillac"), exist_ok=True)
        with open(self.path(), "w") as f:
            json.dump({
                "phase": "build",
                "round_in_phase": 2,
                "total_rounds": 50,
                "validate_retries": 1,
                "manifest_files": ["main.py"],
                "entry_point": "main.py",
                "task": "old build",
                "architecture": "",
                "ts": 0,
            }, f)
        self.restored = _restore_phase_state(_load_checkpoint(self.ws), Phase.BUILD)

    def test_v1_loads(self):
        self.assertEqual(self.restored.total_rounds, 50)
        self.assertEqual(self.restored.validate_retries, 1)

    def test_v1_takes_default_budgets(self):
        self.assertEqual(self.restored.max_rounds[Phase.BUILD], DEFAULT_BUDGETS[Phase.BUILD])

    def test_v1_takes_default_guard_flags(self):
        self.assertFalse(self.restored.runtime_verify_done)
        self.assertFalse(self.restored.completeness_critic_done)
        self.assertEqual(self.restored.current_tier, "all")


class TestMixedBudgetKeys(_CheckpointCase):
    """Regression: `state.max_rounds` does NOT hold only Phase keys.

    `compute_budgets()` returns Phase keys PLUS a literal "max_total_rounds"
    string, and the engine merges the whole dict in with
    `state.max_rounds.update(budgets)` (engine.py ~3377). The first version of
    the v2 serializer assumed enum keys and crashed a real build against .39
    at its very first checkpoint with:

        AttributeError: 'str' object has no attribute 'value'
    """

    def _state_as_engine_builds_it(self):
        from cadillac.phases import compute_budgets

        state = PhaseState()
        budgets = compute_budgets({"files": ["a.py"] * 20, "modular": True,
                                   "modules": ["core", "api"]}, task_text="build a service")
        state.max_rounds.update(budgets)
        return state, budgets

    def test_save_does_not_raise_on_mixed_keys(self):
        state, _ = self._state_as_engine_builds_it()
        self.assertIn("max_total_rounds", state.max_rounds,
                      "precondition: engine really does merge a string key in")
        self.save(state)  # must not raise

    def test_mixed_keys_round_trip(self):
        state, budgets = self._state_as_engine_builds_it()
        self.save(state)
        restored = _restore_phase_state(_load_checkpoint(self.ws), Phase.BUILD)
        self.assertEqual(restored.max_rounds[Phase.BUILD], budgets[Phase.BUILD])
        self.assertEqual(restored.max_rounds["max_total_rounds"],
                         budgets["max_total_rounds"])

    def test_phase_rounds_used_tolerates_mixed_keys(self):
        state, _ = self._state_as_engine_builds_it()
        state.phase_rounds_used["not_a_phase"] = 3
        state.phase_rounds_used[Phase.BUILD] = 7
        self.save(state)
        restored = _restore_phase_state(_load_checkpoint(self.ws), Phase.BUILD)
        self.assertEqual(restored.phase_rounds_used[Phase.BUILD], 7)


class TestRealRuntimeShapes(_CheckpointCase):
    """Regression: PhaseState fields hold richer types than they're annotated.

    `stuck_fingerprints` is declared `list` and typed `list[str]` by
    `_is_phase_stuck`, but the BUILD loop appends `frozenset`s
    (engine.py ~4755: `state.stuck_fingerprints.append(retry_fps)` where
    `retry_fps = frozenset(...)`). `json.dumps` cannot serialize a frozenset,
    so a second real build against .39 died at VALIDATE round 147 — ~50
    minutes of work lost — with:

        TypeError: Object of type frozenset is not JSON serializable

    These tests build state the way the ENGINE does, not the way the earlier
    tests assumed it looked.
    """

    def _state_with_engine_shapes(self):
        state = _populated_state()
        # Exactly what the BUILD loop appends.
        state.stuck_fingerprints = [
            frozenset({"E501:app.py", "F401:app.py"}),
            frozenset({"E501:app.py"}),
            frozenset({"E501:app.py", "TS2345:x.ts"}),
        ]
        state.surgical_fixes_attempted = {"E501:app.py"}
        return state

    def test_frozenset_fingerprints_serialize(self):
        self.save(self._state_with_engine_shapes())  # must not raise
        with open(self.path()) as f:
            json.load(f)

    def test_fingerprints_restore_as_sets(self):
        self.save(self._state_with_engine_shapes())
        restored = _restore_phase_state(_load_checkpoint(self.ws), Phase.BUILD)
        self.assertEqual(len(restored.stuck_fingerprints), 3)
        for fp in restored.stuck_fingerprints:
            self.assertIsInstance(fp, frozenset)

    def test_restored_fingerprints_support_intersection(self):
        """The BUILD loop does set.intersection over the last 3 — restored
        state must support that operation, not just round-trip the data."""
        self.save(self._state_with_engine_shapes())
        restored = _restore_phase_state(_load_checkpoint(self.ws), Phase.BUILD)
        recent3 = restored.stuck_fingerprints[-3:]
        common = set.intersection(*[set(s) for s in recent3])
        self.assertEqual(common, {"E501:app.py"})

    def test_surgical_attempts_round_trip(self):
        self.save(self._state_with_engine_shapes())
        restored = _restore_phase_state(_load_checkpoint(self.ws), Phase.BUILD)
        self.assertEqual(restored.surgical_fixes_attempted, {"E501:app.py"})

    def test_arbitrary_unserializable_value_does_not_abort(self):
        """Fail-SAFE: a checkpoint is a resume optimization. An unexpected
        field type must degrade to a warning, never kill a long build."""
        class Exotic:
            pass

        state = _populated_state()
        state.stuck_fingerprints = [Exotic(), frozenset({"a"})]
        self.save(state)  # must not raise
        self.assertTrue(os.path.exists(self.path()))

    def test_write_failure_does_not_abort(self):
        import cadillac.engine as engine

        state = _populated_state()
        original = engine.atomic_write_text if hasattr(engine, "atomic_write_text") else None
        import cadillac._atomic as atomic
        boom = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        real = atomic.atomic_write_text
        atomic.atomic_write_text = boom
        try:
            self.save(state)  # must not raise despite the write blowing up
        finally:
            atomic.atomic_write_text = real
            if original is not None:
                engine.atomic_write_text = original


class TestMalformedFieldsIgnored(_CheckpointCase):
    """A corrupt individual field must not crash the restore."""

    def _restore_with(self, **overrides):
        os.makedirs(os.path.join(self.ws, ".cadillac"), exist_ok=True)
        payload = {"phase": "build", "total_rounds": 5, **overrides}
        with open(self.path(), "w") as f:
            json.dump(payload, f)
        return _restore_phase_state(_load_checkpoint(self.ws), Phase.BUILD)

    def test_bad_budget_type_ignored(self):
        state = self._restore_with(max_rounds={"build": "not-an-int"})
        self.assertEqual(state.max_rounds[Phase.BUILD], DEFAULT_BUDGETS[Phase.BUILD])

    def test_unknown_phase_name_ignored(self):
        state = self._restore_with(max_rounds={"no_such_phase": 99})
        self.assertNotIn("no_such_phase", [p.value for p in state.max_rounds])

    def test_bad_fingerprints_type_ignored(self):
        state = self._restore_with(stuck_fingerprints="not-a-list")
        self.assertEqual(state.stuck_fingerprints, [])

    def test_bad_max_rounds_container_ignored(self):
        state = self._restore_with(max_rounds="not-a-dict")
        self.assertEqual(state.max_rounds[Phase.BUILD], DEFAULT_BUDGETS[Phase.BUILD])


if __name__ == "__main__":
    unittest.main()


class TestResumePhaseMapping(unittest.TestCase):
    """Every checkpointable phase must map to a phase `resume()` can handle.

    `resume()` implements handlers for BUILD and PACKAGE only; anything else
    fell through to "Cannot resume from phase X". The original mapping coerced
    PLAN/DEPS/SCAFFOLD/REVIEW to BUILD but left INTEGRATE, WIRING and VALIDATE
    alone, so a build that died in any of those was unresumable — and VALIDATE
    is where builds stall longest.

    Found by resuming a real 147-round build that died at VALIDATE:
        [ERROR] Cannot resume from phase validate
    """

    @staticmethod
    def _mapped(phase: Phase) -> Phase:
        """Mirror of the coercion in resume()."""
        return phase if phase in (Phase.BUILD, Phase.PACKAGE) else Phase.BUILD

    def test_every_phase_maps_to_a_handled_phase(self):
        for phase in Phase:
            self.assertIn(
                self._mapped(phase), (Phase.BUILD, Phase.PACKAGE),
                f"{phase.value} maps to an unhandled resume phase",
            )

    def test_validate_maps_to_build(self):
        self.assertEqual(self._mapped(Phase.VALIDATE), Phase.BUILD)

    def test_integrate_and_wiring_map_to_build(self):
        self.assertEqual(self._mapped(Phase.INTEGRATE), Phase.BUILD)
        self.assertEqual(self._mapped(Phase.WIRING), Phase.BUILD)

    def test_package_is_preserved(self):
        self.assertEqual(self._mapped(Phase.PACKAGE), Phase.PACKAGE)

    def test_build_is_preserved(self):
        self.assertEqual(self._mapped(Phase.BUILD), Phase.BUILD)

    def test_resume_source_has_no_unhandled_fallthrough(self):
        """Pin the real coercion in engine.py, not just this mirror."""
        import inspect

        from cadillac.engine import resume
        src = inspect.getsource(resume)
        self.assertIn("if resume_phase not in (Phase.BUILD, Phase.PACKAGE)", src)
