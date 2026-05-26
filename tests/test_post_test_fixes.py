"""Regression tests for the post-stress-test fixes.

Covers:
  1. Executor refuses writes to protected config files post-scaffold.
  2. extract_failure_text catches exit-0 jest FAIL (and ignores non-runner exit-0 output).
  3. ErrorTracker auto-appends a tried_failed scratch entry on escalation and
     surfaces recent failures in its intervention message.
  4. PhaseState.fix_required flips True on retreat_to_build.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from cadillac.manifest import ErrorTracker, FileManifest
from cadillac.phases import PhaseState
from cadillac.scratch import Scratch
from cadillac.tools import (
    PROTECTED_CONFIG_PATHS,
    ToolExecutor,
    extract_failure_text,
)


class TestProtectedConfigWrites(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        self.manifest = FileManifest()
        self.ex = ToolExecutor(self.workspace, self.manifest)
        # Seed a minimal package.json so add_dep has something to edit.
        with open(os.path.join(self.workspace, "package.json"), "w") as f:
            json.dump({"name": "t", "version": "0.0.0", "dependencies": {}}, f)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_file_blocked_post_scaffold(self):
        self.ex.config_writes_allowed = False
        result = self.ex.write_file("package.json", '{"type": "module"}')
        self.assertIn("error", result)
        self.assertIn("protected", result["error"].lower())
        # Disk should be untouched
        with open(os.path.join(self.workspace, "package.json")) as f:
            self.assertNotIn("module", f.read())

    def test_edit_file_blocked_post_scaffold(self):
        # Seed a real tsconfig so we can verify the edit was NOT applied.
        ts_path = os.path.join(self.workspace, "tsconfig.json")
        original = '{"compilerOptions": {"module": "commonjs"}}'
        with open(ts_path, "w") as f:
            f.write(original)
        self.ex.config_writes_allowed = False
        result = self.ex.edit_file("tsconfig.json", [{"old": "commonjs", "new": "esnext"}])
        self.assertIn("error", result)
        self.assertIn("protected", result["error"].lower())
        # Critical: disk unchanged
        with open(ts_path) as f:
            self.assertEqual(f.read(), original)

    def test_line_edit_blocked_post_scaffold(self):
        # Seed a real vite.config.ts so disk-state assertion is meaningful.
        vc_path = os.path.join(self.workspace, "vite.config.ts")
        original = "export default { plugins: [] };\n"
        with open(vc_path, "w") as f:
            f.write(original)
        self.ex.config_writes_allowed = False
        result = self.ex.line_edit("vite.config.ts", 1, 1, "// hacked")
        self.assertIn("error", result)
        self.assertIn("protected", result["error"].lower())
        with open(vc_path) as f:
            self.assertEqual(f.read(), original)

    def test_shell_redirect_blocked_post_scaffold(self):
        self.ex.config_writes_allowed = False
        result = self.ex.run_command("echo '{}' > package.json")
        self.assertEqual(result.get("exit_code"), 1)
        self.assertIn("protected config", result.get("stderr", "").lower())

    def test_cat_redirect_blocked_post_scaffold(self):
        # cat is allowed by the command allowlist, so redirect check is the decisive gate.
        self.ex.config_writes_allowed = False
        result = self.ex.run_command("cat /dev/null > tsconfig.json")
        self.assertEqual(result.get("exit_code"), 1)
        self.assertIn("protected config", result.get("stderr", "").lower())

    def test_writes_allowed_during_scaffold(self):
        # Default state is allowed — scaffold phase must still be able to write.
        self.assertTrue(self.ex.config_writes_allowed)
        new_content = '{"name": "seeded-during-scaffold"}'
        result = self.ex.write_file("package.json", new_content)
        self.assertEqual(result.get("status"), "ok")
        # Verify actual disk write — status=ok alone isn't enough proof
        with open(os.path.join(self.workspace, "package.json")) as f:
            self.assertEqual(f.read(), new_content)

    def test_add_dep_bypasses_guard(self):
        self.ex.config_writes_allowed = False
        result = self.ex.add_dep(name="zod", version="^3.22.0")
        self.assertEqual(result.get("status"), "ok")
        with open(os.path.join(self.workspace, "package.json")) as f:
            pkg = json.load(f)
        self.assertEqual(pkg["dependencies"]["zod"], "^3.22.0")
        self.assertNotIn("type", pkg)  # add_dep also strips the anti-pattern

    def test_add_dep_dev_flag(self):
        self.ex.config_writes_allowed = False
        # Use 'lodash' which is not in APPROVED_VERSIONS → no coercion applies.
        # This test is about the dev-flag wiring, not version policy.
        result = self.ex.add_dep(name="lodash", version="^4.17.0", dev=True)
        self.assertEqual(result.get("kind"), "devDependencies")
        self.assertEqual(result.get("version"), "^4.17.0")
        self.assertNotIn("coerced_from", result)
        with open(os.path.join(self.workspace, "package.json")) as f:
            pkg = json.load(f)
        # Assert dev location + exact version — not just presence
        self.assertEqual(pkg["devDependencies"].get("lodash"), "^4.17.0")
        self.assertNotIn("lodash", pkg.get("dependencies", {}))

    def test_non_config_writes_still_work_post_scaffold(self):
        self.ex.config_writes_allowed = False
        content = "export const x = 1;\n"
        result = self.ex.write_file("src/index.ts", content)
        self.assertEqual(result.get("status"), "ok")
        # Verify file actually on disk with exact content
        disk_path = os.path.join(self.workspace, "src/index.ts")
        self.assertTrue(os.path.exists(disk_path))
        with open(disk_path) as f:
            self.assertEqual(f.read(), content)

    def test_protected_set_includes_expected_files(self):
        self.assertIn("package.json", PROTECTED_CONFIG_PATHS)
        self.assertIn("tsconfig.json", PROTECTED_CONFIG_PATHS)
        self.assertIn("jest.config.js", PROTECTED_CONFIG_PATHS)
        self.assertIn("vite.config.ts", PROTECTED_CONFIG_PATHS)
        self.assertIn("vitest.config.ts", PROTECTED_CONFIG_PATHS)


class TestExtractFailureText(unittest.TestCase):
    def test_exit_nonzero_with_stderr(self):
        result = {"exit_code": 1, "stderr": "SyntaxError: bad", "stdout": ""}
        self.assertIn("SyntaxError", extract_failure_text(result, "python3 main.py"))

    def test_exit_nonzero_with_stdout_only(self):
        # jest/vitest write failures to stdout
        result = {"exit_code": 1, "stderr": "", "stdout": "FAIL src/foo.test.ts\n  ● test"}
        self.assertIn("FAIL", extract_failure_text(result, "jest"))

    def test_exit_zero_jest_fail_still_caught(self):
        result = {"exit_code": 0, "stderr": "", "stdout": "FAIL src/foo.test.ts\n  bad"}
        self.assertIn("FAIL", extract_failure_text(result, "jest --silent"))

    def test_exit_zero_non_runner_ignored(self):
        # ls printing "FAIL" in a filename shouldn't be treated as a failure.
        result = {"exit_code": 0, "stderr": "", "stdout": "FAIL.md\nsrc/"}
        self.assertEqual(extract_failure_text(result, "ls"), "")

    def test_exit_zero_runner_without_markers_ignored(self):
        result = {"exit_code": 0, "stderr": "", "stdout": "PASS all tests (3)"}
        self.assertEqual(extract_failure_text(result, "jest"), "")

    def test_combined_stderr_and_stdout(self):
        result = {"exit_code": 2, "stderr": "tsc warning", "stdout": "error TS2345: X"}
        out = extract_failure_text(result, "tsc --noEmit")
        self.assertIn("tsc warning", out)
        self.assertIn("error TS2345", out)


class TestErrorTrackerAutoScratch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        self.manifest = FileManifest()
        self.scratch = Scratch(self.workspace)
        self.tracker = ErrorTracker(self.manifest)
        self.tracker.scratch = self.scratch
        self.tracker.phase_name = "BUILD"
        self.tracker.round_num = 5

    def tearDown(self):
        self._tmp.cleanup()

    def test_auto_writes_tried_failed_on_escalation(self):
        # Feed the same syntax error twice — syntax has max_retries=2, so the second fires.
        self.tracker.last_command = "python3 main.py --test"
        self.tracker.record("SyntaxError: invalid syntax")
        out = self.tracker.record("SyntaxError: invalid syntax")
        self.assertIsNotNone(out, "intervention should fire on 2nd occurrence")
        text = self.scratch.read()
        self.assertIn("## Tried & Failed", text)
        self.assertIn("syntax", text.lower())
        self.assertIn("main.py", text)  # command tail preserved

    def test_intervention_surfaces_recent_failures(self):
        # Pre-populate a tried_failed entry; verify ErrorTracker surfaces it in next intervention.
        self.scratch.append(
            category="tried_failed", content="prev: tried renaming io.py, broke imports",
            phase="BUILD", round_num=1,
        )
        self.tracker.last_command = "python3 main.py"
        self.tracker.record("SyntaxError: unexpected EOF")
        out = self.tracker.record("SyntaxError: unexpected EOF")
        self.assertIsNotNone(out)
        self.assertIn("YOUR RECENT tried_failed NOTES", out)
        self.assertIn("renaming io.py", out)

    def test_no_scratch_no_crash(self):
        # ErrorTracker without a scratch shouldn't crash — this is the default.
        tracker = ErrorTracker(self.manifest)
        tracker.record("SyntaxError: x")
        out = tracker.record("SyntaxError: x")
        self.assertIsNotNone(out)  # intervention still fires
        self.assertNotIn("YOUR RECENT", out)  # but no surfaced notes


class TestPhaseStateFixRequired(unittest.TestCase):
    def test_fix_required_default_false(self):
        state = PhaseState()
        self.assertFalse(state.fix_required)

    def test_retreat_to_build_sets_fix_required(self):
        state = PhaseState()
        ok = state.retreat_to_build()
        self.assertTrue(ok)
        self.assertTrue(state.fix_required)

    def test_retreat_exhausted_does_not_flip_flag(self):
        state = PhaseState()
        # Exhaust retries
        for _ in range(state.max_validate_retries):
            state.retreat_to_build()
        state.fix_required = False
        ok = state.retreat_to_build()
        self.assertFalse(ok)
        self.assertFalse(state.fix_required)


class TestBatchTrackerNudge(unittest.TestCase):
    """Fix 4: [ALL BATCHES COMPLETE] nudge must use project run_cmd + entry_point,
    not the hardcoded 'python3 main.py'."""

    def _plan(self):
        return {
            "build_order": [{"files": ["a.ts"]}, {"files": ["b.ts"]}],
            "files": [{"path": "a.ts"}, {"path": "b.ts"}],
        }

    def test_nudge_uses_provided_run_cmd_and_entry(self):
        from cadillac.manifest import BatchTracker
        bt = BatchTracker(self._plan(), run_cmd="npx ts-node", entry_point="src/index.ts")
        bt.file_written("a.ts")
        nudge = bt.file_written("b.ts")
        self.assertIsNotNone(nudge)
        self.assertIn("ALL BATCHES COMPLETE", nudge)
        self.assertIn("npx ts-node src/index.ts --test", nudge)
        self.assertNotIn("python3 main.py", nudge)

    def test_nudge_defaults_sensibly_when_args_missing(self):
        from cadillac.manifest import BatchTracker
        bt = BatchTracker(self._plan())
        bt.file_written("a.ts")
        nudge = bt.file_written("b.ts")
        # Full expected command including --test flag — a typo would now fail
        self.assertIn("python3 main.py --test", nudge)

    def test_nudge_reads_run_cmd_from_plan_dict(self):
        from cadillac.manifest import BatchTracker
        plan = self._plan()
        plan["run_cmd"] = "node"
        plan["entry_point"] = "dist/app.js"
        bt = BatchTracker(plan)
        bt.file_written("a.ts")
        nudge = bt.file_written("b.ts")
        self.assertIn("node dist/app.js --test", nudge)


class TestValidateTestSeverityEscalation(unittest.TestCase):
    """Fix 3: 'No test files found' must be severity='error' for substantial projects,
    severity='warning' only for tiny 1-2 file scripts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, path: str, content: str = "# stub\n"):
        full = os.path.join(self.workspace, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    def test_python_no_tests_with_many_sources_is_error(self):
        from cadillac.validate import check_tests
        from cadillac.languages import python_language
        self._write("main.py")
        self._write("models.py")
        self._write("services.py")
        self._write("cli.py")
        results = check_tests(self.workspace, python_language())
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed)
        self.assertEqual(results[0].severity, "error")

    def test_python_no_tests_with_one_source_is_warning(self):
        from cadillac.validate import check_tests
        from cadillac.languages import python_language
        self._write("script.py")
        results = check_tests(self.workspace, python_language())
        self.assertFalse(results[0].passed)
        self.assertEqual(results[0].severity, "warning")

    def test_ts_no_tests_with_many_sources_is_error(self):
        from cadillac.validate import check_tests
        from cadillac.languages import typescript_language
        self._write("src/index.ts")
        self._write("src/routes.ts")
        self._write("src/models.ts")
        self._write("src/store.ts")
        results = check_tests(self.workspace, typescript_language())
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed)
        self.assertEqual(results[0].severity, "error")

    def test_source_counters_exclude_tests_and_boilerplate(self):
        from cadillac.validate import _count_python_source_files, _count_ts_source_files
        self._write("main.py")
        self._write("__init__.py")
        self._write("setup.py")
        self._write("tests/test_main.py")
        self.assertEqual(_count_python_source_files(self.workspace), 1)

        self._write("src/app.ts")
        self._write("src/vite.config.ts")
        self._write("src/app.test.ts")
        self._write("src/__tests__/foo.ts")
        self.assertEqual(_count_ts_source_files(self.workspace), 1)


class TestNodeBoilerplateUnconditional(unittest.TestCase):
    """Fix 1: _write_node_boilerplate must always produce tsconfig.json for node-family,
    even when deps list is empty (plan LLM under-declared)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_deps_still_writes_tsconfig(self):
        from cadillac.engine import _write_node_boilerplate
        from cadillac.languages import typescript_language
        _write_node_boilerplate(self.workspace, [], typescript_language())
        tsconfig = os.path.join(self.workspace, "tsconfig.json")
        pkg = os.path.join(self.workspace, "package.json")
        self.assertTrue(os.path.exists(tsconfig), "tsconfig.json must be created")
        self.assertTrue(os.path.exists(pkg), "package.json must be created")
        with open(tsconfig) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["compilerOptions"]["module"], "commonjs")


class TestBoilerplateNoTestRunner(unittest.TestCase):
    """Fix 1: boilerplate must not hardcode a test runner — LLM picks during SCAFFOLD."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_plain_ts_boilerplate_has_no_jest_config(self):
        from cadillac.engine import _write_node_boilerplate
        from cadillac.languages import typescript_language
        _write_node_boilerplate(self.workspace, [], typescript_language())
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "jest.config.js")),
                         "jest.config.js should not be created — LLM decides runner")

    def test_plain_ts_boilerplate_has_no_ts_jest_devdep(self):
        from cadillac.engine import _write_node_boilerplate
        from cadillac.languages import typescript_language
        _write_node_boilerplate(self.workspace, [], typescript_language())
        with open(os.path.join(self.workspace, "package.json")) as f:
            pkg = json.load(f)
        dev = pkg.get("devDependencies", {})
        self.assertNotIn("ts-jest", dev)
        self.assertNotIn("@types/jest", dev)
        # Universal baseline is still present
        self.assertIn("typescript", dev)
        self.assertIn("ts-node", dev)

    def test_plain_ts_boilerplate_has_no_hardcoded_test_script(self):
        from cadillac.engine import _write_node_boilerplate
        from cadillac.languages import typescript_language
        _write_node_boilerplate(self.workspace, [], typescript_language())
        with open(os.path.join(self.workspace, "package.json")) as f:
            pkg = json.load(f)
        scripts = pkg.get("scripts", {})
        self.assertNotIn("test", scripts,
                         "No 'test' script — LLM must set it during SCAFFOLD with its chosen runner")


class TestFixModeFallback(unittest.TestCase):
    """Fix 2: after 3 rounds in FIX_MODE with no successful edits, force-clear."""

    def test_fix_required_lifecycle(self):
        state = PhaseState()
        # Fresh state: no fix required
        self.assertFalse(state.fix_required)
        self.assertEqual(state.fix_mode_rounds_no_edit, 0)
        # Retreat sets fix_required AND resets counter
        state.retreat_to_build()
        self.assertTrue(state.fix_required)
        self.assertEqual(state.fix_mode_rounds_no_edit, 0)

    def test_retreat_resets_no_edit_counter(self):
        state = PhaseState()
        state.retreat_to_build()
        state.fix_mode_rounds_no_edit = 2
        state.retreat_to_build()  # another retreat
        self.assertEqual(state.fix_mode_rounds_no_edit, 0)

    def test_max_validate_retries_bumped_to_5(self):
        state = PhaseState()
        self.assertEqual(state.max_validate_retries, 5)

    def test_tick_fix_mode_inactive_when_no_fix_required(self):
        state = PhaseState()
        self.assertEqual(state.tick_fix_mode(had_successful_edit=False), "inactive")
        self.assertEqual(state.tick_fix_mode(had_successful_edit=True), "inactive")
        self.assertEqual(state.fix_mode_rounds_no_edit, 0)

    def test_tick_fix_mode_cleared_on_edit(self):
        state = PhaseState()
        state.retreat_to_build()
        self.assertEqual(state.tick_fix_mode(had_successful_edit=True), "cleared")
        self.assertFalse(state.fix_required)
        self.assertEqual(state.fix_mode_rounds_no_edit, 0)

    def test_tick_fix_mode_counts_then_fallback_on_third(self):
        """3 consecutive no-edit rounds must trigger fallback. 1st and 2nd are
        'counting'; 3rd is 'fallback' and fix_required clears."""
        state = PhaseState()
        state.retreat_to_build()
        self.assertEqual(state.tick_fix_mode(had_successful_edit=False), "counting")
        self.assertEqual(state.fix_mode_rounds_no_edit, 1)
        self.assertTrue(state.fix_required)
        self.assertEqual(state.tick_fix_mode(had_successful_edit=False), "counting")
        self.assertEqual(state.fix_mode_rounds_no_edit, 2)
        self.assertTrue(state.fix_required)
        # Third no-edit round triggers fallback
        self.assertEqual(state.tick_fix_mode(had_successful_edit=False), "fallback")
        self.assertFalse(state.fix_required)
        self.assertEqual(state.fix_mode_rounds_no_edit, 0)

    def test_tick_fix_mode_edit_at_round_2_prevents_fallback(self):
        """A successful edit between rounds must reset the counter, preventing
        premature fallback even if more no-edit rounds occur afterward."""
        state = PhaseState()
        state.retreat_to_build()
        state.tick_fix_mode(had_successful_edit=False)  # count=1
        state.tick_fix_mode(had_successful_edit=False)  # count=2
        self.assertEqual(state.fix_mode_rounds_no_edit, 2)
        # Edit lands → fix_required clears; fallback never fired
        self.assertEqual(state.tick_fix_mode(had_successful_edit=True), "cleared")
        self.assertFalse(state.fix_required)
        self.assertEqual(state.fix_mode_rounds_no_edit, 0)


class TestConstraintsInjection(unittest.TestCase):
    """Fix 5: plan.constraints must be pinned into workspace scratch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        self.scratch = Scratch(self.workspace)
        self.emit_calls = []

    def tearDown(self):
        self._tmp.cleanup()

    def _emit(self, kind, **kw):
        self.emit_calls.append((kind, kw))

    def test_constraints_appended_to_scratch_as_reminders(self):
        from cadillac.engine import _inject_plan_constraints
        plan = {"constraints": [
            "MUST use vitest not jest",
            "MUST return 400 on invalid input",
        ]}
        accepted = _inject_plan_constraints(plan, self.scratch, self._emit)
        self.assertEqual(len(accepted), 2)
        text = self.scratch.read()
        self.assertIn("MUST use vitest not jest", text)
        self.assertIn("MUST return 400 on invalid input", text)
        self.assertIn("## Reminders for next phase", text)

    def test_missing_constraints_is_noop(self):
        from cadillac.engine import _inject_plan_constraints
        accepted = _inject_plan_constraints({}, self.scratch, self._emit)
        self.assertEqual(accepted, [])
        self.assertEqual(self.scratch.read(), "")

    def test_empty_constraints_list_is_noop(self):
        from cadillac.engine import _inject_plan_constraints
        accepted = _inject_plan_constraints({"constraints": []}, self.scratch, self._emit)
        self.assertEqual(accepted, [])

    def test_non_string_entries_rejected(self):
        from cadillac.engine import _inject_plan_constraints
        plan = {"constraints": ["MUST do X", "", None, 42, "  MUST Y  "]}
        accepted = _inject_plan_constraints(plan, self.scratch, self._emit)
        # Only valid non-empty strings pass — None/42/empty must be dropped.
        self.assertEqual(len(accepted), 2, f"expected 2 valid constraints, got {accepted}")
        self.assertEqual(set(accepted), {"MUST do X", "MUST Y"})
        text = self.scratch.read()
        self.assertNotIn("None", text, "'None' must NOT leak into scratch as a reminder")
        self.assertNotIn("42", text, "'42' must NOT leak into scratch as a reminder")

    def test_non_list_constraints_noop(self):
        from cadillac.engine import _inject_plan_constraints
        # Defensive: if LLM emits a string instead of list, don't crash
        accepted = _inject_plan_constraints({"constraints": "not-a-list"}, self.scratch, self._emit)
        self.assertEqual(accepted, [])
        self.assertEqual(self.scratch.read(), "")


class TestWorkspaceCollectionCheck(unittest.TestCase):
    """Fix 3: workspace-level collection check catches cross-module import errors."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, path: str, content: str):
        full = os.path.join(self.workspace, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    def test_python_broken_cross_import_is_caught(self):
        from cadillac.engine import _workspace_collection_check
        from cadillac.languages import python_language
        # A test file imports something that doesn't exist in another module
        self._write("core/__init__.py", "")
        self._write("core/models.py", "class Foo:\n    pass\n")
        self._write("tests/__init__.py", "")
        self._write("tests/test_it.py",
                    "from core.models import DoesNotExist\n\ndef test_x():\n    assert True\n")
        ok, out = _workspace_collection_check(self.workspace, python_language())
        self.assertFalse(ok)
        self.assertIn("DoesNotExist", out)

    def test_python_clean_workspace_passes(self):
        from cadillac.engine import _workspace_collection_check
        from cadillac.languages import python_language
        self._write("m.py", "def f(): return 1\n")
        self._write("test_m.py", "from m import f\n\ndef test_f():\n    assert f() == 1\n")
        ok, out = _workspace_collection_check(self.workspace, python_language())
        self.assertTrue(ok, f"expected ok; got: {out[:200]}")

    def test_python_no_tests_at_all_still_passes(self):
        from cadillac.engine import _workspace_collection_check
        from cadillac.languages import python_language
        # pytest exits 5 = "no tests collected"; we treat that as ok (not our concern)
        self._write("m.py", "x = 1\n")
        ok, _ = _workspace_collection_check(self.workspace, python_language())
        self.assertTrue(ok)

    def test_unknown_lang_is_noop(self):
        from cadillac.engine import _workspace_collection_check
        ok, out = _workspace_collection_check(self.workspace, None)
        self.assertTrue(ok)
        self.assertEqual(out, "")


class TestEditSuccessDetection(unittest.TestCase):
    """Fix 2: manifest version deltas detect only SUCCESSFUL edits (not rejected ones)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        self.manifest = FileManifest()
        with open(os.path.join(self.workspace, "package.json"), "w") as f:
            json.dump({"name": "t"}, f)
        self.ex = ToolExecutor(self.workspace, self.manifest)

    def tearDown(self):
        self._tmp.cleanup()

    def _snapshot(self):
        return {p: info["version"] for p, info in self.manifest.files.items()}

    def _has_edit(self, pre):
        return any(
            info["version"] > pre.get(p, 0)
            for p, info in self.manifest.files.items()
        )

    def test_rejected_write_does_not_register_as_edit(self):
        self.ex.config_writes_allowed = False
        pre = self._snapshot()
        result = self.ex.write_file("package.json", '{"type":"module"}')
        self.assertIn("error", result)
        self.assertFalse(self._has_edit(pre))

    def test_successful_write_registers_as_edit(self):
        pre = self._snapshot()
        result = self.ex.write_file("src/app.ts", "export const x = 1;\n")
        self.assertEqual(result.get("status"), "ok")
        self.assertTrue(self._has_edit(pre))


class TestCoerceVersion(unittest.TestCase):
    """Inspector Tier 1: version policy coercion."""

    def test_star_wildcard_coerced(self):
        from cadillac.inspector import coerce_version
        v, coerced = coerce_version("vitest", "*")
        self.assertTrue(coerced)
        self.assertEqual(v, "^1")

    def test_latest_keyword_coerced(self):
        from cadillac.inspector import coerce_version
        v, coerced = coerce_version("vitest", "latest")
        self.assertTrue(coerced)
        self.assertEqual(v, "^1")

    def test_empty_string_coerced(self):
        from cadillac.inspector import coerce_version
        v, coerced = coerce_version("jest", "")
        self.assertTrue(coerced)
        self.assertEqual(v, "^29")

    def test_over_ceiling_major_coerced(self):
        from cadillac.inspector import coerce_version
        v, coerced = coerce_version("vitest", "^3.0.0")
        self.assertTrue(coerced)
        self.assertEqual(v, "^1")

    def test_within_ceiling_passthrough(self):
        from cadillac.inspector import coerce_version
        v, coerced = coerce_version("vitest", "^1.2.3")
        self.assertFalse(coerced)
        self.assertEqual(v, "^1.2.3")

    def test_unknown_package_passthrough(self):
        from cadillac.inspector import coerce_version
        v, coerced = coerce_version("my-cool-pkg", "*")
        self.assertFalse(coerced)
        self.assertEqual(v, "*")

    def test_strict_pin_within_ceiling_passthrough(self):
        """Strict '1.2.3' (no caret) within ceiling is user-intent — respect it."""
        from cadillac.inspector import coerce_version
        v, coerced = coerce_version("vitest", "1.2.3")
        self.assertFalse(coerced)
        self.assertEqual(v, "1.2.3")

    def test_tilde_range_within_ceiling_passthrough(self):
        from cadillac.inspector import coerce_version
        v, coerced = coerce_version("jest", "~29.5.0")
        self.assertFalse(coerced)
        self.assertEqual(v, "~29.5.0")

    def test_tilde_range_over_ceiling_coerced(self):
        from cadillac.inspector import coerce_version
        # ~30.0.0 means 30.x — over the ^29 ceiling for jest
        v, coerced = coerce_version("jest", "~30.0.0")
        self.assertTrue(coerced)
        self.assertEqual(v, "^29")

    def test_ge_range_coerced_as_loose(self):
        """'>=1.0.0' is unbounded above — treated as loose, must pin."""
        from cadillac.inspector import coerce_version
        v, coerced = coerce_version("vitest", ">=1.0.0")
        self.assertTrue(coerced)
        self.assertEqual(v, "^1")


class TestInspectMaterials(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _pkg(self, deps=None, dev=None):
        with open(os.path.join(self.workspace, "package.json"), "w") as f:
            json.dump({
                "name": "t",
                "dependencies": deps or {},
                "devDependencies": dev or {},
            }, f)

    def test_flags_unpinned_vitest(self):
        from cadillac.inspector import inspect_materials
        from cadillac.languages import typescript_language
        self._pkg(dev={"vitest": "*"})
        violations = inspect_materials(self.workspace, typescript_language())
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "dep_unpinned")
        self.assertEqual(violations[0].severity, "error")
        self.assertIn("^1", violations[0].fix)

    def test_passes_pinned_vitest(self):
        from cadillac.inspector import inspect_materials
        from cadillac.languages import typescript_language
        self._pkg(dev={"vitest": "^1.2.0"})
        violations = inspect_materials(self.workspace, typescript_language())
        self.assertEqual(violations, [])

    def test_python_noop(self):
        from cadillac.inspector import inspect_materials
        from cadillac.languages import python_language
        violations = inspect_materials(self.workspace, python_language())
        self.assertEqual(violations, [])


class TestInspectWiring(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, path, content=""):
        full = os.path.join(self.workspace, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    def test_flags_missing_test_script(self):
        from cadillac.inspector import inspect_wiring
        from cadillac.languages import typescript_language
        # Test file exists but no scripts.test
        self._write("src/foo.test.ts", "test('x', ()=>{})")
        self._write("package.json", json.dumps({
            "name": "t",
            "devDependencies": {"vitest": "^1"},
            "scripts": {},
        }))
        violations = inspect_wiring(self.workspace, typescript_language(), plan=None)
        rules = [v.rule for v in violations]
        self.assertIn("missing_test_script", rules)
        v = next(v for v in violations if v.rule == "missing_test_script")
        self.assertIn("vitest", v.fix)  # suggests the installed runner

    def test_missing_test_script_with_no_runner_installed(self):
        from cadillac.inspector import inspect_wiring
        from cadillac.languages import typescript_language
        self._write("src/bar.test.ts", "")
        self._write("package.json", json.dumps({"name": "t", "scripts": {}}))
        violations = inspect_wiring(self.workspace, typescript_language(), plan=None)
        v = next((v for v in violations if v.rule == "missing_test_script"), None)
        self.assertIsNotNone(v)
        self.assertIn("add_dep", v.fix)

    def test_ok_when_test_script_exists(self):
        from cadillac.inspector import inspect_wiring
        from cadillac.languages import typescript_language
        self._write("src/bar.test.ts", "")
        self._write("package.json", json.dumps({
            "name": "t",
            "devDependencies": {"vitest": "^1"},
            "scripts": {"test": "vitest run"},
        }))
        violations = inspect_wiring(self.workspace, typescript_language(), plan=None)
        self.assertEqual(
            [v for v in violations if v.rule == "missing_test_script"], [],
        )

    def test_start_script_missing_file(self):
        from cadillac.inspector import inspect_wiring
        from cadillac.languages import typescript_language
        self._write("package.json", json.dumps({
            "name": "t",
            "scripts": {"start": "npx ts-node src/doesnotexist.ts"},
        }))
        violations = inspect_wiring(self.workspace, typescript_language(), plan=None)
        rules = [v.rule for v in violations]
        self.assertIn("start_script_missing_file", rules)

    def test_entry_point_in_plan_missing(self):
        from cadillac.inspector import inspect_wiring
        from cadillac.languages import python_language
        violations = inspect_wiring(
            self.workspace, python_language(), plan={"entry_point": "missing.py"},
        )
        rules = [v.rule for v in violations]
        self.assertIn("entry_point_missing", rules)


class TestInspectCommissioning(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, path, content):
        full = os.path.join(self.workspace, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    def test_python_cross_module_import_fail(self):
        from cadillac.inspector import inspect_commissioning
        from cadillac.languages import python_language
        self._write("core/__init__.py", "")
        self._write("core/models.py", "class Foo: pass\n")
        self._write("test_it.py",
                    "from core.models import DoesNotExist\ndef test_x(): pass\n")
        violations = inspect_commissioning(self.workspace, python_language(), plan=None)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "collection_error")
        self.assertIn("DoesNotExist", violations[0].detail)

    def test_python_clean_passes(self):
        from cadillac.inspector import inspect_commissioning
        from cadillac.languages import python_language
        self._write("m.py", "x = 1\n")
        self._write("test_m.py", "from m import x\ndef test_x(): assert x == 1\n")
        violations = inspect_commissioning(self.workspace, python_language(), plan=None)
        self.assertEqual(violations, [])


class TestRenderForLlm(unittest.TestCase):
    def test_empty_list_empty_string(self):
        from cadillac.inspector import render_for_llm
        self.assertEqual(render_for_llm([]), "")

    def test_renders_errors_and_warnings(self):
        from cadillac.inspector import render_for_llm, Violation
        out = render_for_llm([
            Violation("r1", "error", "f.json", "Change X to Y", "because Z"),
            Violation("r2", "warning", "g.ts", "Consider W"),
        ])
        self.assertIn("BUILDING-CODE VIOLATIONS", out)
        self.assertIn("[r1]", out)
        self.assertIn("Change X to Y", out)
        self.assertIn("because Z", out)
        self.assertIn("warnings", out)
        self.assertIn("[r2]", out)


class TestAddDepCoercion(unittest.TestCase):
    """add_dep must gate unsafe versions via inspector.coerce_version."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        with open(os.path.join(self.workspace, "package.json"), "w") as f:
            json.dump({"name": "t", "dependencies": {}, "devDependencies": {}}, f)
        self.manifest = FileManifest()
        self.ex = ToolExecutor(self.workspace, self.manifest)
        self.ex.config_writes_allowed = False  # post-scaffold state

    def tearDown(self):
        self._tmp.cleanup()

    def test_vitest_star_coerced(self):
        result = self.ex.add_dep(name="vitest", version="*", dev=True)
        self.assertEqual(result.get("status"), "ok")
        self.assertEqual(result.get("version"), "^1")
        self.assertEqual(result.get("coerced_from"), "*")
        with open(os.path.join(self.workspace, "package.json")) as f:
            pkg = json.load(f)
        self.assertEqual(pkg["devDependencies"]["vitest"], "^1")

    def test_unknown_package_passthrough(self):
        result = self.ex.add_dep(name="lodash", version="*")
        self.assertEqual(result.get("status"), "ok")
        self.assertEqual(result.get("version"), "*")
        self.assertNotIn("coerced_from", result)


class TestBlueprintVerification(unittest.TestCase):
    """Tier 0 blueprint verification — downgrade react→typescript when plan
    doesn't actually use React features."""

    def test_react_stays_react_with_tsx_file(self):
        from cadillac.inspector import verify_language_against_plan
        from cadillac.languages import typescript_language

        class FakeLang:
            name = "react"
            family = "node"
        lang = FakeLang()
        plan = {"files": [{"path": "src/App.tsx"}]}
        out = verify_language_against_plan(lang, plan)
        self.assertIs(out, lang)

    def test_react_stays_react_with_react_dep(self):
        from cadillac.inspector import verify_language_against_plan

        class FakeLang:
            name = "react"
            family = "node"
        lang = FakeLang()
        plan = {"files": [{"path": "src/main.ts"}], "dependencies": ["react", "react-dom"]}
        out = verify_language_against_plan(lang, plan)
        self.assertIs(out, lang)

    def test_react_downgraded_to_typescript_when_no_jsx(self):
        from cadillac.inspector import verify_language_against_plan

        class FakeLang:
            name = "react"
            family = "node"
        lang = FakeLang()
        plan = {"files": [
            {"path": "src/forge.ts"},
            {"path": "src/cli.ts"},
        ], "dependencies": ["vitest", "seedrandom"]}
        out = verify_language_against_plan(lang, plan)
        self.assertEqual(out.name, "typescript")

    def test_typescript_passthrough(self):
        from cadillac.inspector import verify_language_against_plan

        class FakeLang:
            name = "typescript"
            family = "node"
        lang = FakeLang()
        plan = {"files": [{"path": "src/main.ts"}]}
        out = verify_language_against_plan(lang, plan)
        self.assertIs(out, lang)

    def test_react_stays_with_jsx_in_interfaces(self):
        from cadillac.inspector import verify_language_against_plan

        class FakeLang:
            name = "react"
            family = "node"
        lang = FakeLang()
        plan = {"files": [
            {"path": "src/Oracle.ts", "interfaces": ["Oracle: JSX.Element Component"]}
        ]}
        out = verify_language_against_plan(lang, plan)
        self.assertIs(out, lang)

    def test_none_plan_passthrough(self):
        from cadillac.inspector import verify_language_against_plan

        class FakeLang:
            name = "react"
            family = "node"
        lang = FakeLang()
        out = verify_language_against_plan(lang, None)
        self.assertIs(out, lang)


class TestPinnedVersionsBlock(unittest.TestCase):
    """Gap 3 — live package.json pins surfaced as LLM context."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_node_project_renders_pinned_block(self):
        from cadillac.engine import _pinned_versions_block
        from cadillac.languages import typescript_language
        with open(os.path.join(self.workspace, "package.json"), "w") as f:
            json.dump({
                "dependencies": {"express": "^4.18.0"},
                "devDependencies": {"vitest": "^1.6.0", "typescript": "^5.4.2"},
            }, f)
        out = _pinned_versions_block(self.workspace, typescript_language())
        self.assertIn("Current Pinned Dependencies", out)
        self.assertIn("vitest: ^1.6.0", out)
        self.assertIn("express: ^4.18.0", out)
        self.assertIn("TRUST WHAT'S PINNED", out)

    def test_empty_workspace_returns_empty(self):
        from cadillac.engine import _pinned_versions_block
        from cadillac.languages import typescript_language
        out = _pinned_versions_block(self.workspace, typescript_language())
        self.assertEqual(out, "")

    def test_critical_packages_prioritized_in_order(self):
        """Critical packages (vitest, jest, vite) must appear BEFORE filler packages,
        and must survive the 25-entry cap even with 30 fillers ahead alphabetically."""
        from cadillac.engine import _pinned_versions_block
        from cadillac.languages import typescript_language
        deps = {f"pkg{i:02d}": "^1.0" for i in range(30)}  # pkg00..pkg29 (alphabetically before 'vitest')
        deps["vitest"] = "^1.6.0"
        with open(os.path.join(self.workspace, "package.json"), "w") as f:
            json.dump({"devDependencies": deps}, f)
        out = _pinned_versions_block(self.workspace, typescript_language())
        self.assertIn("vitest: ^1.6.0", out, "vitest (critical) must appear despite 30 fillers")
        # Critical package must come BEFORE any non-critical
        vitest_idx = out.index("vitest: ^1.6.0")
        first_pkg_idx = out.index("pkg00")
        self.assertLess(vitest_idx, first_pkg_idx,
                        "critical packages must be listed before non-critical ones")

    def test_python_project_reads_requirements_txt(self):
        from cadillac.engine import _pinned_versions_block
        from cadillac.languages import python_language
        with open(os.path.join(self.workspace, "requirements.txt"), "w") as f:
            f.write("flask==2.3.0\nrequests>=2.28\n# comment\n\n")
        out = _pinned_versions_block(self.workspace, python_language())
        self.assertIn("flask==2.3.0", out)
        self.assertIn("requests>=2.28", out)
        self.assertNotIn("comment", out)


class TestPreValidateCommissioning(unittest.TestCase):
    """Gap 1 — sanity check that _run_inspector supports commissioning tier.

    The actual wiring into VALIDATE start is tested via integration by
    observing no exception in engine import + unit-tested via inspector
    directly; this is the lightweight registry check."""

    def test_run_inspector_commissioning_reports_real_failure(self):
        """Must actually invoke the commissioning tier and surface a real violation —
        not just return an empty list."""
        from cadillac.engine import _run_inspector
        from cadillac.languages import python_language
        import tempfile
        with tempfile.TemporaryDirectory() as w:
            # Create a broken cross-module import that commissioning should catch
            os.makedirs(os.path.join(w, "core"))
            with open(os.path.join(w, "core/__init__.py"), "w") as f:
                f.write("")
            with open(os.path.join(w, "test_bad.py"), "w") as f:
                f.write("from core import DoesNotExistThing\ndef test_x(): pass\n")
            events = []
            messages = []
            violations = _run_inspector(
                "commissioning", w, python_language(), None,
                lambda k, **kw: events.append((k, kw)),
                messages=messages,
            )
            self.assertTrue(len(violations) > 0, "Expected at least one violation from broken workspace")
            self.assertEqual(violations[0].rule, "collection_error")
            # Inspector should have logged events
            log_events = [e for e in events if e[0] == "log"]
            self.assertTrue(any("INSPECTOR commissioning" in e[1].get("msg", "") for e in log_events))
            # Messages should have been injected with the render_for_llm block
            self.assertTrue(any("BUILDING-CODE VIOLATIONS" in m.get("content", "")
                                for m in messages))

    def test_run_inspector_commissioning_clean_workspace_empty(self):
        from cadillac.engine import _run_inspector
        from cadillac.languages import python_language
        import tempfile
        with tempfile.TemporaryDirectory() as w:
            # Clean workspace → no violations
            out = _run_inspector(
                "commissioning", w, python_language(), None,
                lambda k, **kw: None,
            )
            self.assertEqual(out, [])

    def test_run_inspector_unknown_tier_is_noop(self):
        from cadillac.engine import _run_inspector
        out = _run_inspector("nonsense", "/tmp", None, None, lambda k, **kw: None)
        self.assertEqual(out, [])


class TestModuleImportSmoke(unittest.TestCase):
    """Gap 4 — per-module validation must fail if module isn't importable from
    workspace root (catches cross-module rename breakage)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, path, content):
        full = os.path.join(self.workspace, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    def test_importable_module_passes(self):
        from cadillac.validate import run_module_validation
        from cadillac.languages import python_language
        self._write("mymod/__init__.py", "from .core import f\n")
        self._write("mymod/core.py", "def f(): return 1\n")
        results = run_module_validation(self.workspace, "mymod/", None, lang=python_language())
        import_results = [r for r in results if r.name == "imports"]
        self.assertTrue(all(r.passed for r in import_results),
                        f"import checks should pass; got {[r.output for r in import_results]}")

    def test_broken_cross_module_import_fails(self):
        from cadillac.validate import run_module_validation
        from cadillac.languages import python_language
        # mymod imports from a sibling that doesn't exist at workspace root
        self._write("mymod/__init__.py", "from core.random import SeededRNG\n")
        self._write("core/__init__.py", "")  # core exists but no core.random
        results = run_module_validation(self.workspace, "mymod/", None, lang=python_language())
        import_fails = [r for r in results if r.name == "imports" and not r.passed]
        self.assertTrue(any(
            "not importable from workspace root" in (r.output or "")
            for r in import_fails
        ), f"Expected workspace-import failure; got: {[r.output for r in import_fails]}")


class TestIntegrateBudgetScaling(unittest.TestCase):
    """Fix 1 — compute_budgets must scale INTEGRATE with n_modules for modular plans.

    Before the fix, `ModularPlan.to_flat_plan()` didn't emit a "modules" key,
    so compute_budgets saw n_modules=0 → INTEGRATE=20. Re-run of glyphdb logged
    exactly that bug (9 modules → still got 20)."""

    def test_modular_9_modules_gives_50_rounds(self):
        from cadillac.phases import compute_budgets, Phase
        plan = {
            "modular": True,
            "files": [{"path": f"f{i}.py"} for i in range(32)],
            "dependencies": [],
            "modules": [{"name": f"m{i}"} for i in range(9)],
        }
        budgets = compute_budgets(plan)
        # 20 + (9 - 3) * 5 = 50
        self.assertEqual(budgets[Phase.INTEGRATE], 50)

    def test_modular_3_modules_still_20(self):
        from cadillac.phases import compute_budgets, Phase
        plan = {
            "modular": True,
            "files": [{"path": "a.py"}],
            "modules": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
        }
        budgets = compute_budgets(plan)
        # 20 + max(0, 3-3) * 5 = 20
        self.assertEqual(budgets[Phase.INTEGRATE], 20)

    def test_flat_plan_integrate_is_zero(self):
        from cadillac.phases import compute_budgets, Phase
        plan = {"files": [{"path": "main.py"}]}  # no modular key
        budgets = compute_budgets(plan)
        self.assertEqual(budgets[Phase.INTEGRATE], 0)

    def test_to_flat_plan_preserves_modules_for_budgets(self):
        """Regression: ModularPlan.to_flat_plan() must include 'modules' list
        so compute_budgets can scale INTEGRATE correctly."""
        from cadillac.modules import ModularPlan, ModuleSpec
        mp = ModularPlan(
            modules=[
                ModuleSpec(name=f"m{i}", path=f"m{i}/", purpose="", files=[],
                          exports=[], interfaces=[], depends_on=[], build_order=[],
                          test_file="")
                for i in range(5)
            ],
            dependencies=[],
            entry_point="main.py",
            integration_files=["main.py"],
            module_build_order=[["m0", "m1", "m2", "m3", "m4"]],
            test_file="",
        )
        flat = mp.to_flat_plan()
        self.assertIn("modules", flat)
        self.assertEqual(len(flat["modules"]), 5)


class TestCodeMapCrossModuleSurface(unittest.TestCase):
    """Fix 5a + 5b — code map must surface class attributes (enum values) and
    module-level constants so LLM sees cross-module contracts without re-reads."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, path, content):
        full = os.path.join(self.workspace, path)
        os.makedirs(os.path.dirname(full) or self.workspace, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    def _force_skeleton_tier(self):
        """Pad workspace with a few large-source fillers so T1 (full source) is over
        budget, forcing the builder to T2-T4 (skeleton) — but not T5 (too minimal).
        T2-T4 is where our cross-module surface fix applies."""
        for i in range(5):
            # Each filler is ~3000 chars of function bodies
            body = "\n".join(
                f"    x{j} = {j} * 2 + 1  # some padding computation here"
                for j in range(80)
            )
            self._write(f"filler{i}.py", f"def filler_{i}():\n{body}\n    return 0\n")

    def test_code_map_surfaces_enum_values_in_skeleton(self):
        """The glyphdb drift bug: parser uses 'INT' but catalog has 'INTEGER'.
        In SKELETON mode (tier 2+), enum values must still be surfaced so the LLM
        sees cross-module contracts without re-reading the file."""
        from cadillac.codemap import CodeMapBuilder
        self._write("catalog.py", (
            'from enum import Enum\n\n'
            'class ColumnType(Enum):\n'
            '    INTEGER = 1\n'
            '    TEXT = 2\n'
            '    FLOAT = 3\n'
            '    BOOL = 4\n'
            '    def from_string(name: str) -> "ColumnType":\n'
            '        return ColumnType[name]\n'
        ))
        self._force_skeleton_tier()
        builder = CodeMapBuilder(self.workspace, budget_tokens=3000)
        out = builder.build()
        # Assert skeleton tier 2-4 (not full source, not minimal)
        self.assertIn(builder.tier, (2, 3, 4), f"expected skeleton tier 2-4, got {builder.tier}")
        # Enum values MUST be surfaced in skeleton tiers
        self.assertIn("INTEGER = 1", out, f"enum value missing from tier {builder.tier} render")
        self.assertIn("TEXT = 2", out)

    def test_code_map_surfaces_module_constants_in_skeleton(self):
        from cadillac.codemap import CodeMapBuilder
        self._write("storage.py", (
            'PAGE_SIZE = 4096\n'
            'MAGIC_HEADER = 0xCAFEBABE\n'
            'MAX_TUPLE_BYTES = 2048\n\n'
            'def read_page(fd, page_no):\n'
            '    pass\n'
        ))
        self._force_skeleton_tier()
        builder = CodeMapBuilder(self.workspace, budget_tokens=3000)
        out = builder.build()
        self.assertIn(builder.tier, (2, 3, 4), f"expected skeleton tier 2-4, got {builder.tier}")
        self.assertIn("PAGE_SIZE = 4096", out)
        self.assertIn("MAGIC_HEADER", out)

    def test_init_signature_fully_rendered_in_skeleton(self):
        """__init__ must show its full signature + short body preview — this is
        the contract callers at other modules depend on. Regression guard for the
        glyphdb HeapFile(path, layout) 2-vs-3-args drift bug."""
        from cadillac.codemap import CodeMapBuilder
        self._write("persistence.py", (
            'class HeapFile:\n'
            '    """Heap file with page layout."""\n\n'
            '    def __init__(\n'
            '        self,\n'
            '        path: str,\n'
            '        layout: "Layout",\n'
            '    ) -> None:\n'
            '        self.path = path\n'
            '        self.layout = layout\n'
            '        self._fd = None\n\n'
            '    def read_page(self, page_no: int) -> bytes:\n'
            '        return b""\n'
        ))
        self._force_skeleton_tier()
        out = CodeMapBuilder(self.workspace, budget_tokens=3000).build()
        # Full __init__ sig — every arg visible including type hints
        self.assertIn("path: str", out, "full __init__ arg must be in code map")
        self.assertIn('layout: "Layout"', out, "typed arg must show in code map")
        self.assertIn("-> None", out, "return type hint must be in code map")

    def test_module_constants_capped_at_10_in_skeleton_tier(self):
        """Bound prompt size: in skeleton tier (tier 2+), cap constant rendering at 10
        even if a file has 50. Tier 1 shows full source anyway."""
        from cadillac.codemap import CodeMapBuilder
        content = "\n".join(f"CONST_{i} = {i}" for i in range(50)) + "\n"
        self._write("noisy.py", content)
        # Tiny budget forces skeleton tier 2+
        out = CodeMapBuilder(self.workspace, budget_tokens=200).build()
        # Count rendered CONST lines — should be ≤ 10 in skeleton mode
        rendered = sum(1 for ln in out.splitlines() if "CONST_" in ln and "=" in ln)
        self.assertLessEqual(rendered, 10, f"skeleton rendered {rendered} constants (cap is 10)")


class TestPromptBiasForFewerModules(unittest.TestCase):
    """Fix 2 — modular PLAN prompt should bias toward fewer, larger modules."""

    def test_modular_architecture_has_merge_criteria(self):
        """Module guider must give TECHNICAL CRITERIA for merging — not a count."""
        from cadillac.prompts import build_modular_architecture_prompt
        out = build_modular_architecture_prompt()
        # Criteria-based guidance, not raw count target
        self.assertIn("binary/wire/byte-layout contract", out)
        self.assertIn("sequential stages of ONE pipeline", out)
        self.assertIn("TECHNICAL FORCE", out)

    def test_modular_architecture_has_split_criteria(self):
        from cadillac.prompts import build_modular_architecture_prompt
        out = build_modular_architecture_prompt()
        self.assertIn("SPLIT into separate modules when ALL of these hold", out)
        self.assertIn("stable", out)
        self.assertIn("replaced with a different implementation", out)

    def test_modular_architecture_suggests_shared_types_file(self):
        """Shared format/types file is the one-source-of-truth pattern that
        prevents writer/reader drift (the glyphdb WAL 4-vs-5 field bug)."""
        from cadillac.prompts import build_modular_architecture_prompt
        out = build_modular_architecture_prompt()
        self.assertIn("_types.py", out)
        self.assertIn("internal_contracts", out)

    def test_modular_architecture_forbids_shared_types_module(self):
        """Regression guard: v3 built `core_types/` as a dedicated module, which
        just moves drift across a module boundary instead of eliminating it.
        The prompt must explicitly forbid this."""
        from cadillac.prompts import build_modular_architecture_prompt
        out = build_modular_architecture_prompt()
        self.assertIn("never create a dedicated", out)
        self.assertIn("INSIDE that module", out)

    def test_module_scaffold_writes_shared_types_first(self):
        from cadillac.prompts import build_module_scaffold_prompt
        out = build_module_scaffold_prompt(
            module_name="persistence", module_purpose="", module_path="persistence/",
        )
        self.assertIn("SHARED CONTRACTS", out)
        self.assertIn("_types.py", out)
        self.assertIn("WRITE IT FIRST", out)


class TestPytestFirstScaffoldGuidance(unittest.TestCase):
    """Fix 4 — scaffold prompt must discourage monolithic run_tests() functions."""

    def test_scaffold_prompt_pushes_pytest_first(self):
        from cadillac.prompts import build_scaffold_prompt
        out = build_scaffold_prompt()
        # Match the exact phrasing in the template (case sensitive on SEPARATE)
        self.assertIn("SEPARATE pytest file per subsystem", out)
        self.assertIn("monolithic", out)
        self.assertIn("pytest.main", out)

    def test_module_scaffold_prompt_requires_pytest_file(self):
        """Regression: modular builds were skipping pytest files because Fix 4
        only touched the flat _SCAFFOLD_TEMPLATE. Now _MODULE_SCAFFOLD_TEMPLATE
        must also require per-module pytest files."""
        from cadillac.prompts import build_module_scaffold_prompt
        out = build_module_scaffold_prompt(
            module_name="auth", module_purpose="", module_path="auth/",
        )
        self.assertIn("test_auth.py", out, "module scaffold must name the pytest file")
        self.assertIn("def test_", out, "must describe pytest function style")
        self.assertIn("monolithic", out)

    def test_integration_prompt_invokes_pytest(self):
        """Integration entry point's --test must shell out to pytest, not
        re-implement assertions inline."""
        from cadillac.prompts import build_integration_prompt
        out = build_integration_prompt(
            module_summaries="", integration_files="main.py",
            architecture="", entry_point="main.py",
        )
        self.assertIn("pytest.main", out)


class TestComputeReadLimits(unittest.TestCase):
    """Read-file caps should scale with context budget — a 16K-char hard cap on
    a 131K-token model is silent truncation."""

    def test_legacy_fallback_when_unknown(self):
        from cadillac.tools import compute_read_limits
        # None/0/missing → legacy (16000, 5000)
        self.assertEqual(compute_read_limits(None), (16000, 5000))
        self.assertEqual(compute_read_limits(0), (16000, 5000))

    def test_scales_with_context(self):
        from cadillac.tools import compute_read_limits
        # Larger context → larger caps (monotonic)
        small = compute_read_limits(40000)
        medium = compute_read_limits(100000)
        large = compute_read_limits(131072)
        self.assertLess(small[0], medium[0])
        self.assertLess(medium[0], large[0])
        self.assertLess(small[1], medium[1])
        self.assertLess(medium[1], large[1])

    def test_131k_context_unblocks_big_reads(self):
        """The motivating case: 131K model should NOT silently truncate a 70K-char file."""
        from cadillac.tools import compute_read_limits
        chars, lines = compute_read_limits(131072)
        self.assertGreater(chars, 70000, f"131K context should read >70K chars, got {chars}")
        self.assertGreater(lines, 2000)

    def test_ceilings_bound_memory(self):
        """Pathologically large context doesn't blow up memory — caps have ceilings."""
        from cadillac.tools import compute_read_limits
        chars, lines = compute_read_limits(10_000_000)
        self.assertLessEqual(chars, 200_000)
        self.assertLessEqual(lines, 40_000)

    def test_floors_prevent_tiny_regressions(self):
        """Even with a tiny (hypothetical) context, the caps have floors."""
        from cadillac.tools import compute_read_limits
        chars, lines = compute_read_limits(1000)  # silly small
        self.assertGreaterEqual(chars, 8000)
        self.assertGreaterEqual(lines, 1000)

    def test_executor_uses_configured_budget(self):
        """Integration: ToolExecutor with context_budget derives the right caps."""
        from cadillac.tools import ToolExecutor
        from cadillac.manifest import FileManifest
        with tempfile.TemporaryDirectory() as w:
            ex = ToolExecutor(w, FileManifest(), context_budget=131072)
            # 131072 → 78K chars expected
            self.assertGreater(ex._read_chars, 70_000)
            # Legacy default behavior when no budget passed
            ex_legacy = ToolExecutor(w, FileManifest())
            self.assertEqual(ex_legacy._read_chars, 16000)
            self.assertEqual(ex_legacy._read_lines, 5000)


class TestScratchBudget(unittest.TestCase):
    """Scratch caps should scale with context too — fixed 1500/1000/5000 starves
    a 131K-model build of state-visibility."""

    def test_legacy_fallback(self):
        from cadillac.engine import _scratch_budget
        b = _scratch_budget(None)
        self.assertIn("root", b)
        self.assertIn("dep", b)
        self.assertIn("entry", b)
        # Defaults should at least match prior hardcoded values
        self.assertGreaterEqual(b["root"], 1500)
        self.assertGreaterEqual(b["dep"], 500)
        self.assertGreaterEqual(b["entry"], 2000)

    def test_scales_with_context(self):
        from cadillac.engine import _scratch_budget
        small = _scratch_budget(40000, n_modules=3)
        big = _scratch_budget(131072, n_modules=3)
        self.assertGreater(big["entry"], small["entry"])
        self.assertGreater(big["root"], small["root"])

    def test_more_modules_shrinks_per_dep(self):
        """Per-dep cap shrinks as n_modules grows — bounded total scratch."""
        from cadillac.engine import _scratch_budget
        few = _scratch_budget(131072, n_modules=2)
        many = _scratch_budget(131072, n_modules=10)
        self.assertGreater(few["dep"], many["dep"])
        # But never goes below the floor
        self.assertGreaterEqual(many["dep"], 500)

    def test_ceilings_prevent_runaway(self):
        """Absurd context doesn't produce absurd budgets."""
        from cadillac.engine import _scratch_budget
        b = _scratch_budget(10_000_000, n_modules=1)
        # 5% of 10M = 500K chars × 4 × 0.05 = 2M → capped
        self.assertLessEqual(sum(b.values()), 100_000)  # combined cap sanity


class TestPythonEntryStub(unittest.TestCase):
    """Foundation-time: plan.entry_point exists on disk before SCAFFOLD starts,
    so LLM edits it naturally instead of inventing cli/main.py. Parallel to how
    _write_node_boilerplate writes package.json + tsconfig.json."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_stub_when_missing(self):
        from cadillac.engine import _write_python_entry_stub
        wrote = _write_python_entry_stub(self.workspace, "glyphdb.py")
        self.assertTrue(wrote)
        path = os.path.join(self.workspace, "glyphdb.py")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            content = f.read()
        # Stub must have the essentials
        self.assertIn("def main(", content)
        self.assertIn('if __name__ == "__main__":', content)
        self.assertIn("sys.exit(main())", content)
        # And a TODO marker so LLM knows to fill it in
        self.assertIn("TODO", content)

    def test_no_op_when_exists(self):
        """Don't overwrite if LLM or prior run already wrote something."""
        from cadillac.engine import _write_python_entry_stub
        path = os.path.join(self.workspace, "existing.py")
        with open(path, "w") as f:
            f.write("# handcrafted content\nprint('hi')\n")
        original = open(path).read()
        wrote = _write_python_entry_stub(self.workspace, "existing.py")
        self.assertFalse(wrote)
        self.assertEqual(open(path).read(), original)  # unchanged

    def test_creates_nested_dir(self):
        from cadillac.engine import _write_python_entry_stub
        wrote = _write_python_entry_stub(self.workspace, "src/pkg/app.py")
        self.assertTrue(wrote)
        self.assertTrue(os.path.exists(os.path.join(self.workspace, "src/pkg/app.py")))

    def test_stub_actually_runs(self):
        """The stub should be runnable out of the box (exit 0)."""
        import subprocess
        from cadillac.engine import _write_python_entry_stub
        _write_python_entry_stub(self.workspace, "app.py")
        r = subprocess.run(["python3", "app.py"], cwd=self.workspace,
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0, f"stub didn't run cleanly: {r.stderr}")

    def test_empty_entry_path_noop(self):
        from cadillac.engine import _write_python_entry_stub
        self.assertFalse(_write_python_entry_stub(self.workspace, ""))
        self.assertFalse(_write_python_entry_stub(self.workspace, None))


class TestAutoShimEntryPoint(unittest.TestCase):
    """Last-chance rescue for the v1-v6 pattern: LLM puts CLI main in
    cli/main.py but plan declared entry is glyphdb.py at root. Inspector fires
    14×, LLM ignores. Fix: auto-write a 5-line shim at declared path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        self.events = []

    def tearDown(self):
        self._tmp.cleanup()

    def _emit(self, kind, **kw):
        self.events.append((kind, kw))

    def _write(self, path, content):
        full = os.path.join(self.workspace, path)
        os.makedirs(os.path.dirname(full) or self.workspace, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    def test_shim_written_when_entry_missing(self):
        from cadillac.engine import _auto_shim_entry_point
        from cadillac.languages import python_language
        self._write("cli/main.py", (
            "def main():\n"
            "    return 0\n\n"
            'if __name__ == "__main__":\n'
            "    main()\n"
        ))
        self._write("cli/__init__.py", "")
        wrote = _auto_shim_entry_point(self.workspace, python_language(), "glyphdb.py", self._emit)
        self.assertTrue(wrote)
        shim_path = os.path.join(self.workspace, "glyphdb.py")
        self.assertTrue(os.path.exists(shim_path))
        with open(shim_path) as f:
            shim = f.read()
        self.assertIn("from cli.main import main", shim)
        self.assertIn("sys.exit", shim)

    def test_no_shim_when_entry_exists(self):
        from cadillac.engine import _auto_shim_entry_point
        from cadillac.languages import python_language
        # Already-correct project — shim must be a no-op
        self._write("glyphdb.py", "print('hello')\n")
        original_bytes = os.path.getsize(os.path.join(self.workspace, "glyphdb.py"))
        wrote = _auto_shim_entry_point(self.workspace, python_language(), "glyphdb.py", self._emit)
        self.assertFalse(wrote)
        # File unchanged
        self.assertEqual(os.path.getsize(os.path.join(self.workspace, "glyphdb.py")), original_bytes)

    def test_no_shim_when_no_main_found(self):
        from cadillac.engine import _auto_shim_entry_point
        from cadillac.languages import python_language
        # Workspace has code but no `def main()` anywhere — we don't invent one
        self._write("lib/helpers.py", "def helper():\n    pass\n")
        wrote = _auto_shim_entry_point(self.workspace, python_language(), "glyphdb.py", self._emit)
        self.assertFalse(wrote)
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "glyphdb.py")))

    def test_prefers_cli_dir_main_file(self):
        """When multiple main()s exist, prefer cli/main.py style."""
        from cadillac.engine import _auto_shim_entry_point
        from cadillac.languages import python_language
        # Decoy main in a utils folder
        self._write("utils/helper.py", "def main():\n    return 1\n")
        # Real main in cli/main.py
        self._write("cli/main.py", (
            "def main():\n    return 0\n\n"
            'if __name__ == "__main__":\n    main()\n'
        ))
        wrote = _auto_shim_entry_point(self.workspace, python_language(), "app.py", self._emit)
        self.assertTrue(wrote)
        with open(os.path.join(self.workspace, "app.py")) as f:
            shim = f.read()
        self.assertIn("from cli.main import main", shim)
        self.assertNotIn("from utils.helper", shim)

    def test_skips_test_files(self):
        from cadillac.engine import _auto_shim_entry_point
        from cadillac.languages import python_language
        # Only file with a main is a test file — don't shim to it
        self._write("test_foo.py", "def main():\n    return 0\n")
        wrote = _auto_shim_entry_point(self.workspace, python_language(), "run.py", self._emit)
        self.assertFalse(wrote)

    def test_python_only_node_skipped(self):
        from cadillac.engine import _auto_shim_entry_point
        from cadillac.languages import typescript_language
        self._write("src/cli.ts", "export function main() {}\n")
        wrote = _auto_shim_entry_point(self.workspace, typescript_language(), "src/index.ts", self._emit)
        self.assertFalse(wrote)  # Python-only for now

    def test_shim_emits_log(self):
        from cadillac.engine import _auto_shim_entry_point
        from cadillac.languages import python_language
        self._write("cli/main.py", "def main():\n    return 0\n")
        _auto_shim_entry_point(self.workspace, python_language(), "prog.py", self._emit)
        logs = [msg for kind, kw in self.events if kind == "log" for msg in [kw.get("msg", "")]]
        self.assertTrue(any("[auto-shim]" in m for m in logs))


class TestComputeMaxOut(unittest.TestCase):
    """_compute_max_out must never let input + max_out exceed the window.

    Regression from a real v5 error: input_est=114729 + max_out=16384 = 131113
    against context_window=131112 (one token over). Root cause: the 256-token
    safety margin was too thin to absorb char-based estimator undercount."""

    def test_user_observed_edge_case(self):
        from cadillac.engine import _compute_max_out
        # The exact scenario that hit the hard limit
        out = _compute_max_out(context_window=131112, input_est=114729)
        total = 114729 + out
        self.assertLess(total, 131112,
                        f"input+max_out must stay under window; got {total} vs 131112")
        # Our safety margin is 2048, so we should be well under
        self.assertLessEqual(114729 + out + 2048, 131112 + 2048)

    def test_headroom_cases(self):
        """Across a range of realistic input sizes, input + max_out + margin
        must never exceed the window."""
        from cadillac.engine import _compute_max_out, _CHAT_SAFETY_MARGIN
        for win in (32768, 65536, 131072, 200000):
            for input_est in (win // 4, win // 2, win - 20000, win - 5000):
                out = _compute_max_out(win, input_est)
                self.assertLessEqual(
                    input_est + out + _CHAT_SAFETY_MARGIN, win,
                    f"win={win} input={input_est} out={out} — exceeds window+margin",
                )

    def test_hard_cap_at_16384(self):
        from cadillac.engine import _compute_max_out
        # Plenty of room → caps at the hardcoded 16K output limit
        self.assertEqual(_compute_max_out(131072, 10000), 16384)

    def test_floor_when_too_little_room(self):
        """When input is very close to ceiling, we still return the floor.
        vLLM will reject cleanly if it actually can't fit — we don't."""
        from cadillac.engine import _compute_max_out, _MAX_OUT_FLOOR
        # input_est only 500 below window → available = -1548 → clamped to floor
        out = _compute_max_out(131072, 131072 - 500)
        self.assertEqual(out, _MAX_OUT_FLOOR)

    def test_safety_margin_absorbs_small_undercount(self):
        """If the char-estimator undercounts by 500 tokens, we still stay under
        the hard window limit thanks to the 2048 margin."""
        from cadillac.engine import _compute_max_out
        input_est_underestimate = 114729
        real_input = input_est_underestimate + 500  # typical BPE vs char delta
        out = _compute_max_out(131112, input_est_underestimate)
        self.assertLess(real_input + out, 131112,
                        "2048 margin must absorb up to ~1500 estimator undercount")


class TestRateLimit(unittest.TestCase):
    """`--rate` / _pace() ensures we don't stampede the vLLM server."""

    def setUp(self):
        from cadillac.engine import _RATE_STATE
        _RATE_STATE.clear()

    def test_pace_sleeps_when_below_interval(self):
        """If elapsed time < 1/rps, _pace should sleep the remainder."""
        from cadillac.engine import _pace, _RATE_STATE
        url = "http://fake.local/v1"
        # Prime state with a recent send
        _RATE_STATE[url] = 100.0  # last send at t=100
        slept = []
        with mock.patch("time.time", side_effect=[100.5, 100.5, 104.0]), \
             mock.patch("time.sleep", side_effect=lambda s: slept.append(s)):
            _pace(url, rps=0.25)  # min_interval = 4s, only 0.5s elapsed → sleep 3.5s
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 3.5, places=2)

    def test_pace_no_sleep_when_interval_elapsed(self):
        from cadillac.engine import _pace, _RATE_STATE
        url = "http://fake.local/v1"
        _RATE_STATE[url] = 100.0
        slept = []
        # elapsed = 10s, min_interval at rps=0.25 is 4s → no sleep
        with mock.patch("time.time", side_effect=[110.0, 110.0]), \
             mock.patch("time.sleep", side_effect=lambda s: slept.append(s)):
            _pace(url, rps=0.25)
        self.assertEqual(slept, [])

    def test_pace_zero_disables(self):
        from cadillac.engine import _pace, _RATE_STATE
        _RATE_STATE["http://x/v1"] = 100.0
        slept = []
        with mock.patch("time.time", return_value=100.0), \
             mock.patch("time.sleep", side_effect=lambda s: slept.append(s)):
            _pace("http://x/v1", rps=0)  # disabled
            _pace("http://x/v1", rps=-1)  # also disabled
            _pace("http://x/v1", rps=None)  # also disabled
        self.assertEqual(slept, [])

    def test_pace_per_endpoint_isolated(self):
        """Two endpoints should have independent pacing — one being busy doesn't
        affect the other."""
        from cadillac.engine import _pace, _RATE_STATE
        _RATE_STATE["http://a/v1"] = 100.0  # just sent
        slept = []
        with mock.patch("time.time", side_effect=[100.1, 100.1]), \
             mock.patch("time.sleep", side_effect=lambda s: slept.append(s)):
            # Different endpoint → no prior timestamp → no sleep
            _pace("http://b/v1", rps=0.25)
        self.assertEqual(slept, [])

    def test_config_default_rate_limit(self):
        from cadillac.engine import Config
        # Clear env so we see the code default, not a test env leak
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CADILLAC_RATE_LIMIT", None)
            cfg = Config()
            self.assertAlmostEqual(cfg.rate_limit, 0.25, places=3)

    def test_config_rate_limit_from_env(self):
        from cadillac.engine import Config
        with mock.patch.dict(os.environ, {"CADILLAC_RATE_LIMIT": "1.5"}):
            cfg = Config()
            self.assertAlmostEqual(cfg.rate_limit, 1.5, places=3)


class TestReadFileArgAliases(unittest.TestCase):
    """read_file accepts both start_line/end_line and line_start/line_end.

    The 35B Qwen3.6 consistently used start_line/end_line (matching our
    line_edit tool's naming) — our schema was the outlier. 34 tool errors in
    one build wasted rounds. Fix: accept both conventions."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        self.manifest = FileManifest()
        self.ex = ToolExecutor(self.workspace, self.manifest)
        content = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
        self.ex.build_mode = True  # bypass "you wrote this" guard
        self.ex.write_file("f.txt", content)

    def tearDown(self):
        self._tmp.cleanup()

    def test_legacy_line_start_line_end_still_works(self):
        r = self.ex.read_file("f.txt", line_start=3, line_end=5)
        self.assertEqual(r.get("lines"), "3-5")
        self.assertIn("line3", r["content"])
        self.assertIn("line5", r["content"])

    def test_new_start_line_end_line_works(self):
        r = self.ex.read_file("f.txt", start_line=3, end_line=5)
        self.assertEqual(r.get("lines"), "3-5")
        self.assertIn("line3", r["content"])
        self.assertIn("line5", r["content"])

    def test_start_line_wins_when_both_given(self):
        """If both naming conventions are passed with different values,
        start_line/end_line (the primary) take precedence."""
        r = self.ex.read_file("f.txt", start_line=10, end_line=12,
                              line_start=1, line_end=2)
        self.assertEqual(r.get("lines"), "10-12")

    def test_offset_limit_still_works(self):
        r = self.ex.read_file("f.txt", offset=5, limit=3)
        self.assertEqual(r.get("lines"), "5-7")

    def test_dispatch_via_tool_call_with_end_line(self):
        """End-to-end: simulate an LLM tool call with start_line/end_line
        (string values, as JSON often arrives)."""
        r = self.ex.dispatch("read_file", {"path": "f.txt", "start_line": "3", "end_line": "5"})
        self.assertEqual(r.get("lines"), "3-5")

    def test_read_file_schema_advertises_new_names(self):
        from cadillac.tools import TOOL_DEFS
        read_schema = next(t for t in TOOL_DEFS if t["function"]["name"] == "read_file")
        props = read_schema["function"]["parameters"]["properties"]
        self.assertIn("start_line", props)
        self.assertIn("end_line", props)


class TestContextAuto(unittest.TestCase):
    """--context-auto flag: detect context window from /v1/models with headroom."""

    def test_compute_context_budget_large_window(self):
        from cadillac.cadillac import compute_context_budget
        # 131K (Qwen3.6-35B) → 131072 - 20000 - 5% = 131072 - 20000 - 6553 = 104519
        self.assertEqual(compute_context_budget(131072), 131072 - 20000 - int(131072 * 0.05))
        # 100K → 100000 - 20000 - 5000 = 75000
        self.assertEqual(compute_context_budget(100000), 75000)

    def test_compute_context_budget_small_window(self):
        from cadillac.cadillac import compute_context_budget
        # For ≤32K windows, fall back to 60% (20K+5% reserve would be too aggressive).
        self.assertEqual(compute_context_budget(16384), int(16384 * 0.60))
        self.assertEqual(compute_context_budget(32768), int(32768 * 0.60))

    def test_compute_context_budget_has_5pct_headroom_vs_legacy(self):
        """Regression guard: v5 hit ~92K input on a 131K window (within 9% of
        ceiling). The formula must leave at least (20K + 5% of window) reserve
        so peak bursts don't blow past max_tokens."""
        from cadillac.cadillac import compute_context_budget
        for window in (50000, 100000, 131072, 200000):
            budget = compute_context_budget(window)
            reserve = window - budget
            expected_min_reserve = 20000 + int(window * 0.05)
            self.assertGreaterEqual(reserve, expected_min_reserve,
                                    f"window={window}: reserve={reserve}, expected ≥{expected_min_reserve}")

    def test_compute_context_budget_threshold_transition(self):
        """Right above 32K, the fixed-reserve formula kicks in."""
        from cadillac.cadillac import compute_context_budget
        # At 32768: 60% rule → 19660
        self.assertEqual(compute_context_budget(32768), int(32768 * 0.60))
        # Formula monotonicity: bigger window ⇒ bigger budget
        self.assertLess(compute_context_budget(50000), compute_context_budget(100000))

    def test_detect_context_window_success(self):
        from cadillac import cadillac as cad
        import io, json
        class FakeResp:
            def __init__(self, payload): self.p = payload
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.p
        payload = json.dumps({"data": [{"max_model_len": 131112}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(payload)):
            # 192.0.2.x is TEST-NET-1 (RFC 5737), reserved for docs/examples
            got = cad._detect_context_window("http://192.0.2.42:8000/v1")
        self.assertEqual(got, 131112)

    def test_detect_context_window_network_failure(self):
        """On any failure (timeout, 404, JSON parse error), return None, don't raise."""
        from cadillac import cadillac as cad
        with mock.patch("urllib.request.urlopen", side_effect=ConnectionError("boom")):
            self.assertIsNone(cad._detect_context_window("http://bogus.invalid/v1"))
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()):
            self.assertIsNone(cad._detect_context_window("http://slow.invalid/v1"))

    def test_detect_context_window_malformed_response(self):
        from cadillac import cadillac as cad
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"wrong": "shape"}'
        with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
            self.assertIsNone(cad._detect_context_window("http://x.invalid/v1"))


class TestPhaseHistoryMemory(unittest.TestCase):
    """`record_phase_outcome` + `recall_phase_stats` persist and read back
    cross-build round counts used by `compute_budgets`."""

    def setUp(self):
        from cadillac import memory
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False)
        self._tmp.close()
        self._orig_path = memory.PHASE_HISTORY_PATH
        memory.PHASE_HISTORY_PATH = self._tmp.name

    def tearDown(self):
        from cadillac import memory
        memory.PHASE_HISTORY_PATH = self._orig_path
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_empty_history_returns_none(self):
        from cadillac import memory
        self.assertIsNone(memory.recall_phase_stats("build"))

    def test_record_and_recall_roundtrip(self):
        from cadillac import memory
        for rounds in (10, 12, 14, 30, 50):
            memory.record_phase_outcome("build", rounds, 5, ["python"])
        stats = memory.recall_phase_stats("build")
        self.assertIsNotNone(stats)
        self.assertEqual(stats["count"], 5)
        self.assertEqual(stats["max"], 50)
        # p90 of [10,12,14,30,50] at idx=4 → 50
        self.assertEqual(stats["p90"], 50)

    def test_min_samples_gate(self):
        # With only 2 samples we return None — one outlier shouldn't balloon
        # the next build's budget.
        from cadillac import memory
        memory.record_phase_outcome("build", 10, 5)
        memory.record_phase_outcome("build", 12, 5)
        self.assertIsNone(memory.recall_phase_stats("build"))

    def test_tag_filter_prefers_matches(self):
        # Heavy pygame builds shouldn't affect a React task's budget.
        from cadillac import memory
        for r in (40, 45, 50, 55):
            memory.record_phase_outcome("build", r, 10, ["python", "pygame"])
        for r in (8, 10, 12):
            memory.record_phase_outcome("build", r, 5, ["typescript", "react"])
        stats = memory.recall_phase_stats("build", tags=["react"])
        self.assertIsNotNone(stats)
        self.assertLessEqual(stats["p90"], 12)

    def test_tag_mismatch_returns_none_not_fallback(self):
        # Prod bug caught end-to-end: a TS task was getting BUILD=78 from
        # pygame history via a tag-fallback. Wrong signal is worse than no
        # signal — unrelated task types must not inherit budgets.
        from cadillac import memory
        for r in (20, 25, 30):
            memory.record_phase_outcome("build", r, 5, ["python", "pygame"])
        self.assertIsNone(memory.recall_phase_stats("build", tags=["rust"]))

    def test_untagged_task_sees_all_history(self):
        # When the caller provides no tags (task_text empty), every recorded
        # run is a fair signal — no bucket to restrict to.
        from cadillac import memory
        for r in (20, 25, 30):
            memory.record_phase_outcome("build", r, 5, ["python"])
        stats = memory.recall_phase_stats("build", tags=None)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["count"], 3)

    def test_compute_budgets_expands_from_history(self):
        from cadillac import memory
        from cadillac.phases import compute_budgets, Phase
        # Past pygame builds took 40-50 BUILD rounds.
        for r in (40, 42, 45, 48, 50):
            memory.record_phase_outcome(
                Phase.BUILD.value, r, 8, ["python", "pygame"])
        plan = {"files": ["a.py"] * 3, "dependencies": []}
        # Baseline would be max(20, 3*4)=20. History should push up above that.
        budgets = compute_budgets(plan, task_text="build a pygame game")
        self.assertGreater(budgets[Phase.BUILD], 20)

    def test_compute_budgets_never_shrinks_from_history(self):
        from cadillac import memory
        from cadillac.phases import compute_budgets, Phase
        # Past runs finished fast — baseline should still win.
        for r in (5, 6, 7, 8):
            memory.record_phase_outcome(Phase.BUILD.value, r, 2, ["python"])
        plan = {"files": ["a.py"] * 20, "dependencies": []}
        budgets = compute_budgets(plan, task_text="python cli")
        # Baseline BUILD = 20*4=80 — history's p90 (8) must NOT shrink it.
        self.assertGreaterEqual(budgets[Phase.BUILD], 80)

    def test_compute_budgets_no_task_text_is_safe(self):
        # Back-compat: old callers that don't pass task_text still work.
        from cadillac.phases import compute_budgets, Phase
        plan = {"files": ["a.py"], "dependencies": []}
        budgets = compute_budgets(plan)
        self.assertIn(Phase.BUILD, budgets)

    def test_compute_budgets_unrelated_task_does_not_inherit(self):
        # Prod-bug regression: a rust task with a pygame-heavy history file
        # used to inherit 78-round BUILD budgets via tag-fallback. Now it
        # must stay at baseline.
        from cadillac import memory
        from cadillac.phases import compute_budgets, Phase
        for r in (55, 60, 65):
            memory.record_phase_outcome(
                Phase.BUILD.value, r, 3, ["python", "pygame"])
        plan = {"files": ["main.rs", "lib.rs"], "dependencies": []}
        # "rust web server" has no known-tag words → no bucket match → baseline.
        budgets = compute_budgets(plan, task_text="rust web server")
        self.assertEqual(budgets[Phase.BUILD], 20)

    def test_compute_budgets_no_task_text_skips_history(self):
        # Empty task_text is the "caller didn't tell us what this is" signal.
        # Don't let unrelated past budgets leak in.
        from cadillac import memory
        from cadillac.phases import compute_budgets, Phase
        for r in (55, 60, 65):
            memory.record_phase_outcome(
                Phase.BUILD.value, r, 3, ["python"])
        plan = {"files": ["a.py"], "dependencies": []}
        budgets = compute_budgets(plan, task_text="")
        self.assertEqual(budgets[Phase.BUILD], 20)

    def test_compute_budgets_constraint_bump(self):
        # Many explicit constraints → BUILD needs extra rounds to enforce all.
        from cadillac.phases import compute_budgets, Phase
        plan_light = {"files": ["a.py"], "constraints": ["c1", "c2"]}
        plan_heavy = {"files": ["a.py"], "constraints": [f"c{i}" for i in range(10)]}
        b_light = compute_budgets(plan_light, task_text="x")
        b_heavy = compute_budgets(plan_heavy, task_text="x")
        self.assertGreater(b_heavy[Phase.BUILD], b_light[Phase.BUILD])


class TestPhaseStateRoundsTracking(unittest.TestCase):
    """PhaseState.advance captures rounds for end-of-build history write."""

    def test_advance_records_prior_rounds(self):
        from cadillac.phases import Phase, PhaseState
        s = PhaseState()
        s.round_in_phase = 3
        s.advance()  # PLAN → DEPS
        self.assertEqual(s.phase_rounds_used.get(Phase.PLAN), 3)
        s.round_in_phase = 5
        s.advance()
        self.assertEqual(s.phase_rounds_used.get(Phase.DEPS), 5)

    def test_retreat_then_advance_accumulates(self):
        # retreat_to_plan + re-advance should add to the prior count, not
        # overwrite — each round was really spent.
        from cadillac.phases import Phase, PhaseState
        s = PhaseState()
        s.round_in_phase = 2
        s.advance()  # PLAN → DEPS (2 rounds recorded)
        s.retreat_to_plan()
        s.round_in_phase = 3
        s.advance()  # PLAN → DEPS again (3 more)
        self.assertEqual(s.phase_rounds_used.get(Phase.PLAN), 5)


class TestApprovedVersionsForHost(unittest.TestCase):
    """`approved_versions_for_host` auto-detects Node major and picks the
    safest matching row — so a host upgrade relaxes ceilings with no code edit."""

    def setUp(self):
        from cadillac import inspector
        # Each test gets a clean slate — otherwise the first subprocess probe
        # poisons later mock-based assertions.
        inspector._host_node_major_cache = None
        inspector._host_versions_cache = None

    def tearDown(self):
        # Restore real-host detection for other test classes that reach into
        # APPROVED_VERSIONS (e.g. TestCoerceVersion, TestInspectMaterials).
        from cadillac import inspector
        inspector._host_node_major_cache = None
        inspector._host_versions_cache = None

    def _mock_node_version(self, stdout: str, returncode: int = 0):
        class FakeResult:
            def __init__(self, stdout, returncode):
                self.stdout = stdout
                self.returncode = returncode
        return mock.patch(
            "cadillac.inspector.subprocess.run",
            return_value=FakeResult(stdout, returncode),
        )

    def test_node_18_picks_legacy_ceilings(self):
        from cadillac import inspector
        with self._mock_node_version("v18.19.1\n"):
            row = inspector.approved_versions_for_host()
        self.assertEqual(row["vitest"], "^1")
        self.assertEqual(row["vite"], "^5")
        self.assertEqual(row["jest"], "^29")

    def test_node_20_relaxes_ceilings(self):
        from cadillac import inspector
        with self._mock_node_version("v20.11.0\n"):
            row = inspector.approved_versions_for_host()
        self.assertEqual(row["vitest"], "^2")
        self.assertEqual(row["vite"], "^6")

    def test_node_22_further_relaxed(self):
        from cadillac import inspector
        with self._mock_node_version("v22.3.0\n"):
            row = inspector.approved_versions_for_host()
        self.assertEqual(row["vitest"], "^3")
        self.assertEqual(row["vite"], "^7")
        self.assertEqual(row["jest"], "^30")

    def test_node_21_falls_back_to_20_row(self):
        # Odd-numbered majors aren't in the table — pick the highest that is
        # ≤ host. Node 21 → 20 row.
        from cadillac import inspector
        with self._mock_node_version("v21.5.0\n"):
            row = inspector.approved_versions_for_host()
        self.assertEqual(row["vitest"], "^2")

    def test_missing_node_falls_back_to_default(self):
        # No Node installed → default to the safest (lowest) row, never the
        # newest, so we don't ship ^7 vite to a Node-18 box.
        from cadillac import inspector
        with mock.patch(
            "cadillac.inspector.subprocess.run",
            side_effect=FileNotFoundError("no node"),
        ):
            row = inspector.approved_versions_for_host()
        self.assertEqual(row["vitest"], "^1")

    def test_malformed_output_falls_back(self):
        from cadillac import inspector
        with self._mock_node_version("garbage\n"):
            row = inspector.approved_versions_for_host()
        self.assertEqual(row["vitest"], "^1")

    def test_result_is_cached(self):
        # Second call must not invoke subprocess again — startup probes should
        # be one-shot.
        from cadillac import inspector
        with self._mock_node_version("v20.0.0\n") as m:
            inspector.approved_versions_for_host()
            inspector.approved_versions_for_host()
            inspector.approved_versions_for_host()
        self.assertEqual(m.call_count, 1)

    def test_proxy_supports_legacy_dict_api(self):
        # `APPROVED_VERSIONS` has long been a `dict` — callers use `[x]`, `in`,
        # `.get`, iteration. The proxy MUST preserve all four.
        from cadillac import inspector
        with self._mock_node_version("v18.0.0\n"):
            self.assertIn("vitest", inspector.APPROVED_VERSIONS)
            self.assertEqual(inspector.APPROVED_VERSIONS["vitest"], "^1")
            self.assertEqual(inspector.APPROVED_VERSIONS.get("nope"), None)
            self.assertGreater(len(list(inspector.APPROVED_VERSIONS)), 0)

    def test_coerce_version_uses_host_row(self):
        # End-to-end: on Node 20, a pinned `vite: ^5` should stay (within
        # ceiling) and `vite: ^8` should coerce to ^6.
        from cadillac import inspector
        with self._mock_node_version("v20.0.0\n"):
            _, coerced5 = inspector.coerce_version("vite", "^5")
            coerced_str, coerced8 = inspector.coerce_version("vite", "^8")
        self.assertFalse(coerced5)
        self.assertTrue(coerced8)
        self.assertEqual(coerced_str, "^6")


class TestEnhanceCodeMapBuilderImport(unittest.TestCase):
    """Prod regression: `cadillac enhance` crashed with NameError: name
    'CodeMapBuilder' is not defined at engine.py:4259. Function referenced
    the class without importing it. This test guards against the symbol-
    reference-without-import shape coming back."""

    def test_enhance_imports_codemapbuilder(self):
        import inspect
        from cadillac.engine import enhance
        src = inspect.getsource(enhance)
        # Either a direct import or use of the module prefix must appear
        # before the first CodeMapBuilder use.
        self.assertIn("CodeMapBuilder", src)
        self.assertIn("from .codemap import CodeMapBuilder", src)

    def test_enhance_callable(self):
        # Just importing it must not raise. Any missing-symbol issue would
        # surface at import-time only for module-level refs; this catches
        # syntax-level regressions in the function body.
        from cadillac.engine import enhance
        self.assertTrue(callable(enhance))


class TestCmdKind(unittest.TestCase):
    """`_cmd_kind` classifies commands into buckets for per-kind history lookup."""

    def test_bare_command(self):
        from cadillac.tools import _cmd_kind
        self.assertEqual(_cmd_kind("pytest"), "pytest")
        self.assertEqual(_cmd_kind("python3 foo.py"), "python3")

    def test_npx_unwraps_to_runner(self):
        from cadillac.tools import _cmd_kind
        # Avoid bucketing every `npx` call together — vitest runs very
        # differently than tsc.
        self.assertEqual(_cmd_kind("npx vitest run"), "vitest")
        self.assertEqual(_cmd_kind("npx tsc --noEmit"), "tsc")
        self.assertEqual(_cmd_kind("npx eslint ."), "eslint")

    def test_npm_test_script(self):
        from cadillac.tools import _cmd_kind
        self.assertEqual(_cmd_kind("npm test"), "npm-test")
        self.assertEqual(_cmd_kind("npm run build"), "npm-run")
        self.assertEqual(_cmd_kind("npm start"), "npm-start")

    def test_empty_command(self):
        from cadillac.tools import _cmd_kind
        self.assertEqual(_cmd_kind(""), "unknown")
        self.assertEqual(_cmd_kind("   "), "unknown")


class TestAdaptiveTimeout(unittest.TestCase):
    """`_adaptive_timeout` scales wall-clock budget from recorded history."""

    def test_cold_workspace_returns_baseline(self):
        # No history → behave exactly like the old hardcoded literal. This is
        # the "no regression on first run" guarantee.
        from cadillac.tools import _adaptive_timeout
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(_adaptive_timeout("pytest", ws, baseline=30), 30)
            self.assertEqual(_adaptive_timeout("npx vitest", ws, baseline=120), 120)

    def test_scales_from_history(self):
        # If pytest took 40s last time, next call should budget ≥ 40*2.5 = 100s,
        # not stick at the 30s baseline and time out.
        from cadillac.tools import _adaptive_timeout, _record_cmd_history
        with tempfile.TemporaryDirectory() as ws:
            for _ in range(5):
                _record_cmd_history(ws, "pytest", 40000, 0)
            t = _adaptive_timeout("pytest", ws, baseline=30)
            self.assertGreaterEqual(t, 100)
            self.assertLessEqual(t, 600)

    def test_baseline_wins_when_history_is_fast(self):
        # Fast past runs shouldn't SHRINK the timeout below baseline — we want
        # headroom for newly-added tests.
        from cadillac.tools import _adaptive_timeout, _record_cmd_history
        with tempfile.TemporaryDirectory() as ws:
            for _ in range(5):
                _record_cmd_history(ws, "pytest", 2000, 0)
            t = _adaptive_timeout("pytest", ws, baseline=30)
            self.assertEqual(t, 30)

    def test_ceiling_clamp(self):
        # Runaway history mustn't produce a 10-minute+ wait.
        from cadillac.tools import _adaptive_timeout, _record_cmd_history
        with tempfile.TemporaryDirectory() as ws:
            for _ in range(5):
                _record_cmd_history(ws, "pytest", 999_999_000, 0)
            t = _adaptive_timeout("pytest", ws, baseline=30)
            self.assertLessEqual(t, 600)

    def test_different_kinds_isolated(self):
        # Slow pytest history shouldn't balloon vitest's budget.
        from cadillac.tools import _adaptive_timeout, _record_cmd_history
        with tempfile.TemporaryDirectory() as ws:
            for _ in range(5):
                _record_cmd_history(ws, "pytest", 300000, 0)
            t_vitest = _adaptive_timeout("npx vitest", ws, baseline=30)
            self.assertEqual(t_vitest, 30)

    def test_timeout_gets_recorded_for_next_run(self):
        # Validate.py `_run` records (effective_timeout, exit=124) on timeout,
        # so the NEXT call gets a larger budget instead of timing out again.
        from cadillac.tools import _adaptive_timeout, _record_cmd_history
        with tempfile.TemporaryDirectory() as ws:
            _record_cmd_history(ws, "pytest", 30000, 124)
            t = _adaptive_timeout("pytest", ws, baseline=30)
            self.assertGreaterEqual(t, 30)


class TestValidateRunAdaptive(unittest.TestCase):
    """validate._run wires through _adaptive_timeout and records history."""

    def test_run_records_history(self):
        import subprocess
        from cadillac.validate import _run
        with tempfile.TemporaryDirectory() as ws:
            # Use a trivially-fast allowed command (subprocess.run directly, no
            # sandbox gating applies to validate._run).
            r = _run(["python3", "-c", "print(1)"], cwd=ws, timeout=10)
            self.assertEqual(r.returncode, 0)
            hist_path = os.path.join(ws, ".cadillac", "cmd_history.jsonl")
            self.assertTrue(os.path.exists(hist_path))
            with open(hist_path) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["cmd"], "python3")
            self.assertEqual(lines[0]["exit"], 0)


class TestPipPep668Rewrite(unittest.TestCase):
    """Ubuntu 24.04+ ships an EXTERNALLY-MANAGED marker that breaks plain
    `pip install`. The tool executor auto-adds --break-system-packages so
    the LLM's commands don't silently fail in a stuck loop."""

    def _force_pep668(self, on: bool):
        from cadillac import tools
        tools._PEP668_DETECTED = on
        return tools

    def tearDown(self):
        # Reset cache so real-host detection kicks in for other tests.
        from cadillac import tools
        tools._PEP668_DETECTED = None

    def test_rewrites_plain_pip_install(self):
        tools = self._force_pep668(True)
        cmd = tools._rewrite_pip_for_pep668("pip install flask")
        self.assertIn("--break-system-packages", cmd)

    def test_rewrites_pip3_and_python3_m_pip(self):
        tools = self._force_pep668(True)
        for raw, label in [
            ("pip3 install requests", "pip3"),
            ("python3 -m pip install pytest", "python3 -m pip"),
            ("pip install -q flask click pytest", "pip -q"),
        ]:
            out = tools._rewrite_pip_for_pep668(raw)
            self.assertIn("--break-system-packages", out, f"failed on {label}")

    def test_non_pep668_host_is_noop(self):
        # Debian 11, older Ubuntu, macOS: no marker → no rewrite.
        tools = self._force_pep668(False)
        cmd = tools._rewrite_pip_for_pep668("pip install flask")
        self.assertEqual(cmd, "pip install flask")

    def test_already_flagged_not_duplicated(self):
        # If the LLM already added --break-system-packages, don't double it.
        tools = self._force_pep668(True)
        orig = "pip install flask --break-system-packages"
        self.assertEqual(tools._rewrite_pip_for_pep668(orig), orig)

    def test_user_scope_respected(self):
        # `--user` opts out of system site-packages entirely; no marker
        # conflict, so leave the command untouched.
        tools = self._force_pep668(True)
        orig = "pip install --user flask"
        self.assertEqual(tools._rewrite_pip_for_pep668(orig), orig)

    def test_venv_context_is_noop(self):
        # Inside an activated venv (VIRTUAL_ENV set), pip doesn't hit the
        # system marker — don't add a flag that's wrong for venv pip.
        tools = self._force_pep668(True)
        with mock.patch.dict(os.environ, {"VIRTUAL_ENV": "/tmp/venv"}):
            cmd = tools._rewrite_pip_for_pep668("pip install flask")
        self.assertEqual(cmd, "pip install flask")

    def test_non_install_commands_untouched(self):
        # `pip list`, `pip show`, etc. work fine — don't touch them.
        tools = self._force_pep668(True)
        for raw in ("pip list", "pip show flask", "pip --version",
                    "python3 script.py", "ls pip install README"):
            self.assertEqual(tools._rewrite_pip_for_pep668(raw), raw,
                             f"regression on {raw!r}")


class TestSigtermGracefulShutdown(unittest.TestCase):
    """SIGTERM must unwind the phase loop cleanly so the summary block
    still writes phase_budgets history for the partial build. Without this,
    `pkill` loses every row — exactly when we most want the data (slow or
    broken builds are the ones worth learning from)."""

    def test_run_registers_and_restores_sigterm(self):
        # run() should replace SIGTERM with a raise-on-signal handler and
        # restore the original when done.
        import inspect
        from cadillac.engine import run
        src = inspect.getsource(run)
        self.assertIn("signal.signal(signal.SIGTERM, _sigterm_handler)", src)
        self.assertIn("signal.signal(signal.SIGTERM, _orig_sigterm)", src)

    def test_keyboard_interrupt_caught_and_summary_runs(self):
        # The phase-loop wrapper catches KeyboardInterrupt (what SIGTERM
        # raises) so the summary block after it can still execute.
        import inspect
        from cadillac.engine import run
        src = inspect.getsource(run)
        self.assertIn("except KeyboardInterrupt", src)
        # Must be inside the try/except that wraps the phase loop, and the
        # summary block must be AFTER the finally.
        ki_idx = src.index("except KeyboardInterrupt")
        summary_idx = src.index("# ── Summary ──")
        self.assertLess(ki_idx, summary_idx)

    def test_non_main_thread_skips_registration(self):
        # Parallel callers may invoke run() from a worker thread, where
        # signal.signal() raises ValueError. The wrapper must tolerate that.
        import inspect
        from cadillac.engine import run
        src = inspect.getsource(run)
        self.assertIn("except (ValueError, OSError)", src)


class TestModularRoundsRollup(unittest.TestCase):
    """Per-module rounds must roll up into state.phase_rounds_used so the
    cross-build phase history reflects the REAL work modular builds do,
    not the 1-round top-level phase snapshot that advance() sees."""

    def test_build_module_signature_returns_rounds(self):
        # Guards against signature regression: _build_module must return a
        # 3-tuple, third element is dict with scaffold+build keys.
        import inspect
        from cadillac.engine import _build_module
        src = inspect.getsource(_build_module)
        self.assertIn("rounds_used = {", src)
        self.assertIn("return False, [f\"Module", src)  # first return site
        self.assertIn('return False, error_msgs, rounds_used', src)
        self.assertIn('return True, [], rounds_used', src)

    def test_wave_aggregates_rounds(self):
        # The wave aggregator must sum scaffold+build across modules.
        import inspect
        from cadillac.engine import _build_module_wave
        src = inspect.getsource(_build_module_wave)
        self.assertIn('wave_rounds = {"scaffold": 0, "build": 0}', src)
        self.assertIn("wave_rounds[k] += rounds.get(k, 0)", src)

    def test_phase_state_accepts_rollup(self):
        # The container we write into must accept arbitrary integer additions.
        from cadillac.phases import Phase, PhaseState
        s = PhaseState()
        # Simulate modular SCAFFOLD skipping advance() — rollup writes directly.
        s.phase_rounds_used[Phase.SCAFFOLD] = (
            s.phase_rounds_used.get(Phase.SCAFFOLD, 0) + 42
        )
        s.phase_rounds_used[Phase.BUILD] = (
            s.phase_rounds_used.get(Phase.BUILD, 0) + 87
        )
        self.assertEqual(s.phase_rounds_used[Phase.SCAFFOLD], 42)
        self.assertEqual(s.phase_rounds_used[Phase.BUILD], 87)


class TestEndpointUnreachableAbort(unittest.TestCase):
    """chat() raises EndpointUnreachable after 3 pure ConnectionError attempts.

    Prod context: when an LLM endpoint dies mid-build, cadillac used to keep
    looping through rounds with sentinel empty messages. Now it aborts once.
    """

    def _mock_cfg(self):
        from cadillac.engine import Config
        # Dummy config — tests never actually hit the network (requests.post mocked).
        c = Config()
        c.api_url = "http://127.0.0.1:9"  # deliberately invalid
        c.model = "test-model"
        c.context_window = 4096
        c.stream = False
        c.api_key = None
        c.rate_limit = 0
        return c

    def test_three_connection_errors_raise(self):
        import requests
        from cadillac.engine import chat, EndpointUnreachable
        with mock.patch("cadillac.engine.requests.post") as post, \
             mock.patch("cadillac.engine.time.sleep"):
            post.side_effect = requests.exceptions.ConnectionError("refused")
            with self.assertRaises(EndpointUnreachable):
                chat(self._mock_cfg(), [{"role": "user", "content": "hi"}])
            # Must have tried all 3 attempts before raising (gives endpoint
            # grace; 9s of retry buffer in prod).
            self.assertEqual(post.call_count, 3)

    def test_transient_timeout_still_returns_sentinel(self):
        # Timeouts = endpoint alive-but-slow. Must NOT raise — the next round
        # might succeed, and burning one round on sentinel is acceptable.
        import requests
        from cadillac.engine import chat
        with mock.patch("cadillac.engine.requests.post") as post, \
             mock.patch("cadillac.engine.time.sleep"):
            post.side_effect = requests.exceptions.Timeout("slow")
            result = chat(self._mock_cfg(), [{"role": "user", "content": "hi"}])
        self.assertEqual(result, {"role": "assistant", "content": ""})

    def test_one_connection_error_then_success(self):
        # Transient blip: 1× ConnectionError followed by success must succeed
        # cleanly, not raise. This is the common case on RTX 4090 when vLLM
        # briefly OOMs between rounds.
        import requests
        from cadillac.engine import chat
        class FakeResp:
            status_code = 200
            def json(self):
                return {"choices": [{"message": {"role": "assistant",
                                                  "content": "ok"},
                                      "finish_reason": "stop"}]}
        with mock.patch("cadillac.engine.requests.post") as post, \
             mock.patch("cadillac.engine.time.sleep"):
            post.side_effect = [
                requests.exceptions.ConnectionError("blip"),
                FakeResp(),
            ]
            result = chat(self._mock_cfg(), [{"role": "user", "content": "hi"}])
        self.assertEqual(result.get("content"), "ok")

    def test_mixed_failures_returns_sentinel(self):
        # One ConnectionError then one Timeout means the endpoint WAS reachable
        # at some point — don't escalate to EndpointUnreachable.
        import requests
        from cadillac.engine import chat
        with mock.patch("cadillac.engine.requests.post") as post, \
             mock.patch("cadillac.engine.time.sleep"):
            post.side_effect = [
                requests.exceptions.ConnectionError("gone"),
                requests.exceptions.Timeout("slow"),
            ]
            result = chat(self._mock_cfg(), [{"role": "user", "content": "hi"}])
        self.assertEqual(result, {"role": "assistant", "content": ""})

    def test_http_400_path_still_sanitizes(self):
        # Existing 400-retry sanitization (drops poison tool-call messages)
        # must survive untouched — that's its own separate recovery path.
        from cadillac.engine import chat
        class FakeResp400:
            status_code = 400
            text = "invalid message"
        class FakeResp200:
            status_code = 200
            def json(self):
                return {"choices": [{"message": {"role": "assistant",
                                                  "content": "fixed"},
                                      "finish_reason": "stop"}]}
        # Need >4 messages for the 400 sanitizer to kick in.
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
            {"role": "tool", "content": "t"},
            {"role": "assistant", "content": "a2"},
        ]
        with mock.patch("cadillac.engine.requests.post") as post, \
             mock.patch("cadillac.engine.time.sleep"):
            post.side_effect = [FakeResp400(), FakeResp200()]
            result = chat(self._mock_cfg(), messages)
        self.assertEqual(result.get("content"), "fixed")


class TestStripJsExtensionsSkipsNestedNodeModules(unittest.TestCase):
    """_strip_js_extensions_from_ts must not descend into nested node_modules.

    Prod bug: full-stack layout has frontend/node_modules/ where the walk's
    rel_root is "frontend/node_modules/...", which startswith("node_modules")
    returns False for. It then recorded 90+ transitive .d.ts files into the
    manifest, poisoning every subsequent LLM code-map with noise."""

    def test_nested_node_modules_not_recorded(self):
        from cadillac.engine import _strip_js_extensions_from_ts
        from cadillac.manifest import FileManifest
        with tempfile.TemporaryDirectory() as ws:
            # Frontend project with a legitimate TS file...
            os.makedirs(os.path.join(ws, "frontend/src"))
            with open(os.path.join(ws, "frontend/src/app.ts"), "w") as f:
                f.write("import foo from './bar.js';\n")
            # ...and a typical nested node_modules with a .d.ts file that
            # MUST NOT be walked.
            os.makedirs(os.path.join(ws, "frontend/node_modules/vitest/dist"))
            with open(os.path.join(ws, "frontend/node_modules/vitest/dist/index.d.ts"), "w") as f:
                f.write("export {};\n")
            manifest = FileManifest()
            _strip_js_extensions_from_ts(ws, manifest=manifest)
            recorded = list(manifest.files.keys())
            # The legitimate file may or may not be recorded (only recorded
            # if modified). The node_modules file MUST NOT be recorded.
            for path in recorded:
                self.assertNotIn("node_modules", path,
                    f"node_modules leak: {path!r}")

    def test_dist_also_skipped(self):
        from cadillac.engine import _strip_js_extensions_from_ts
        from cadillac.manifest import FileManifest
        with tempfile.TemporaryDirectory() as ws:
            os.makedirs(os.path.join(ws, "frontend/dist"))
            with open(os.path.join(ws, "frontend/dist/bundle.ts"), "w") as f:
                f.write("import foo from './bar.js';\n")
            manifest = FileManifest()
            _strip_js_extensions_from_ts(ws, manifest=manifest)
            for path in manifest.files.keys():
                self.assertNotIn("dist", path.split(os.sep))


class TestFindNodeProjectDir(unittest.TestCase):
    """_find_node_project_dir locates the dir with package.json + node_modules.

    Prod bug: `npx vitest run` with cwd=workspace (full-stack layout) falls
    through to the global npm cache because local node_modules is in
    frontend/. That cache held vitest@0.34.6, which ignored the LLM's
    pinned vitest@^1.2.0 and hung the build in a "0 test" loop."""

    def test_workspace_root_with_both(self):
        from cadillac.validate import _find_node_project_dir
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "package.json"), "w") as f:
                f.write("{}")
            os.makedirs(os.path.join(ws, "node_modules"))
            self.assertEqual(_find_node_project_dir(ws), ws)

    def test_nested_frontend_wins(self):
        from cadillac.validate import _find_node_project_dir
        with tempfile.TemporaryDirectory() as ws:
            # Backend has a loose package.json (e.g. for an npm script)
            # but no node_modules — not the test runner target.
            os.makedirs(os.path.join(ws, "backend"))
            with open(os.path.join(ws, "backend/package.json"), "w") as f:
                f.write("{}")
            # Frontend has both → it's the real node project.
            os.makedirs(os.path.join(ws, "frontend/node_modules"))
            with open(os.path.join(ws, "frontend/package.json"), "w") as f:
                f.write("{}")
            self.assertEqual(
                _find_node_project_dir(ws), os.path.join(ws, "frontend"))

    def test_only_package_json_fallback(self):
        # Pre-install state: package.json exists but no node_modules yet.
        # Fall back to the subdir rather than workspace root (better than
        # nothing for `npm install` invocations).
        from cadillac.validate import _find_node_project_dir
        with tempfile.TemporaryDirectory() as ws:
            os.makedirs(os.path.join(ws, "frontend"))
            with open(os.path.join(ws, "frontend/package.json"), "w") as f:
                f.write("{}")
            self.assertEqual(
                _find_node_project_dir(ws), os.path.join(ws, "frontend"))

    def test_nothing_found_returns_workspace(self):
        from cadillac.validate import _find_node_project_dir
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(_find_node_project_dir(ws), ws)

    def test_type_module_beats_node_modules(self):
        # Prod regression: root has package.json + node_modules but no
        # type:module; frontend has package.json with type:module + vite.
        # Running `vite build` from root triggers the CJS-deprecated warning
        # that trips our own validator. Frontend must win this scoring.
        from cadillac.validate import _find_node_project_dir
        with tempfile.TemporaryDirectory() as ws:
            # Root: package.json + node_modules, but plain CJS
            with open(os.path.join(ws, "package.json"), "w") as f:
                json.dump({"dependencies": {"express": "^4"}}, f)
            os.makedirs(os.path.join(ws, "node_modules"))
            # Frontend: package.json with type:module + vite (ESM-native)
            os.makedirs(os.path.join(ws, "frontend"))
            with open(os.path.join(ws, "frontend/package.json"), "w") as f:
                json.dump({
                    "type": "module",
                    "devDependencies": {"vite": "^5", "vitest": "^1"},
                }, f)
            self.assertEqual(
                _find_node_project_dir(ws),
                os.path.join(ws, "frontend"))

    def test_tie_prefers_workspace_root(self):
        # When signals are equal, don't surprise-jump into a subdir.
        from cadillac.validate import _find_node_project_dir
        with tempfile.TemporaryDirectory() as ws:
            # Both have identical minimal package.json — no differentiators.
            for loc in (ws, os.path.join(ws, "packages")):
                os.makedirs(loc, exist_ok=True)
                with open(os.path.join(loc, "package.json"), "w") as f:
                    json.dump({}, f)
            self.assertEqual(_find_node_project_dir(ws), ws)


class TestNpxNoInstallForcesLocal(unittest.TestCase):
    """npx without --no-install silently falls back to ~/.npm/_npx/<hash>/,
    which ignores the version pinned in package.json. _npx_no_install rewrites
    any npx invocation to force local-only resolution."""

    def test_plain_npx_gets_no_install(self):
        from cadillac.validate import _npx_no_install
        self.assertEqual(
            _npx_no_install(["npx", "vitest", "run"]),
            ["npx", "--no-install", "vitest", "run"])

    def test_existing_flag_preserved(self):
        # If someone already passed --yes or --prefer-online, respect it.
        from cadillac.validate import _npx_no_install
        cmd = ["npx", "--yes", "create-react-app"]
        self.assertEqual(_npx_no_install(cmd), cmd)

    def test_non_npx_unchanged(self):
        from cadillac.validate import _npx_no_install
        cmd = ["node", "server.js"]
        self.assertEqual(_npx_no_install(cmd), cmd)

    def test_empty_command_safe(self):
        from cadillac.validate import _npx_no_install
        self.assertEqual(_npx_no_install([]), [])


class TestProtectPackageJsonRespectsVite(unittest.TestCase):
    """Anti-pattern: unconditionally stripping `type: module` broke Vite
    builds. Now we only strip when the project actually uses a CJS test
    runner (jest+ts-jest); for Vite/vitest/React projects, `type: module`
    is required to silence the CJS-deprecation warning."""

    def _write_pkg(self, ws, content):
        with open(os.path.join(ws, "package.json"), "w") as f:
            json.dump(content, f)

    def _read_pkg(self, ws):
        with open(os.path.join(ws, "package.json")) as f:
            return json.load(f)

    def test_vite_project_keeps_type_module(self):
        # Regression: memory #4 strip broke the recipe build's vite step.
        from cadillac.engine import _protect_package_json
        from cadillac.languages import typescript_language
        lang = typescript_language()
        with tempfile.TemporaryDirectory() as ws:
            self._write_pkg(ws, {
                "type": "module",
                "devDependencies": {"vite": "^5", "vitest": "^1"},
            })
            modified = _protect_package_json(ws, lang)
            self.assertFalse(modified)
            self.assertEqual(self._read_pkg(ws).get("type"), "module")

    def test_react_framework_keeps_type_module(self):
        # React via Vite is ESM-native; type:module must stay even if no
        # vite dependency is declared at the moment (lang name is authoritative).
        from cadillac.engine import _protect_package_json
        from cadillac.languages import react_language
        lang = react_language()
        with tempfile.TemporaryDirectory() as ws:
            self._write_pkg(ws, {"type": "module", "dependencies": {"react": "^18"}})
            modified = _protect_package_json(ws, lang)
            self.assertFalse(modified)
            self.assertEqual(self._read_pkg(ws).get("type"), "module")

    def test_jest_project_still_stripped(self):
        # Original behavior preserved: plain TS + jest = CJS, type:module
        # breaks jest.config.js import → must strip.
        from cadillac.engine import _protect_package_json
        from cadillac.languages import typescript_language
        lang = typescript_language()
        with tempfile.TemporaryDirectory() as ws:
            self._write_pkg(ws, {
                "type": "module",
                "devDependencies": {"jest": "^29", "ts-jest": "^29"},
            })
            modified = _protect_package_json(ws, lang)
            self.assertTrue(modified)
            self.assertNotIn("type", self._read_pkg(ws))

    def test_no_type_field_no_op(self):
        from cadillac.engine import _protect_package_json
        from cadillac.languages import typescript_language
        lang = typescript_language()
        with tempfile.TemporaryDirectory() as ws:
            self._write_pkg(ws, {"devDependencies": {"jest": "^29"}})
            self.assertFalse(_protect_package_json(ws, lang))


class TestPartitionDepsByEcosystem(unittest.TestCase):
    """Full-stack plans blend npm + Python deps in one list. Without routing,
    npm install gets asked to fetch `flask` and errors 404 (regression from
    recipe build #3)."""

    def test_pure_npm(self):
        from cadillac.engine import _partition_deps_by_ecosystem
        npm, py = _partition_deps_by_ecosystem(["react", "axios", "vite"])
        self.assertEqual(npm, ["react", "axios", "vite"])
        self.assertEqual(py, [])

    def test_pure_python(self):
        from cadillac.engine import _partition_deps_by_ecosystem
        npm, py = _partition_deps_by_ecosystem(["flask", "pytest", "sqlalchemy"])
        self.assertEqual(npm, [])
        self.assertEqual(py, ["flask", "pytest", "sqlalchemy"])

    def test_full_stack_mix(self):
        # Prod regression: this was the exact set recipe build #3 had.
        from cadillac.engine import _partition_deps_by_ecosystem
        npm, py = _partition_deps_by_ecosystem([
            "flask", "flask-cors", "flask-sqlalchemy", "pytest",
            "axios", "react-router-dom", "vite-plugin-react",
        ])
        self.assertIn("axios", npm)
        self.assertIn("react-router-dom", npm)
        self.assertIn("flask", py)
        self.assertIn("flask-cors", py)
        self.assertIn("flask-sqlalchemy", py)
        self.assertIn("pytest", py)
        self.assertNotIn("flask", npm)
        self.assertNotIn("pytest", npm)

    def test_case_insensitive(self):
        from cadillac.engine import _partition_deps_by_ecosystem
        npm, py = _partition_deps_by_ecosystem(["Flask", "PYTEST"])
        self.assertEqual(npm, [])
        # Original casing preserved even though detection is case-insensitive.
        self.assertEqual(py, ["Flask", "PYTEST"])

    def test_empty_strings_skipped(self):
        from cadillac.engine import _partition_deps_by_ecosystem
        npm, py = _partition_deps_by_ecosystem(["", None, "axios"] if False else ["", "axios"])
        self.assertEqual(npm, ["axios"])
        self.assertEqual(py, [])

    def test_unknown_defaults_to_npm(self):
        # Caller is already in the Node boilerplate path, so unknown names
        # default to npm (safer: scoped @foo/bar names, new libs).
        from cadillac.engine import _partition_deps_by_ecosystem
        npm, py = _partition_deps_by_ecosystem(["some-obscure-npm-pkg", "@scope/thing"])
        self.assertEqual(npm, ["some-obscure-npm-pkg", "@scope/thing"])
        self.assertEqual(py, [])


class TestWriteNodeBoilerplateFiltersPython(unittest.TestCase):
    """End-to-end: _write_node_boilerplate routes Python deps to
    requirements.txt instead of poisoning package.json."""

    def test_mixed_deps_split_correctly(self):
        from cadillac.engine import _write_node_boilerplate
        from cadillac.languages import react_language
        with tempfile.TemporaryDirectory() as ws:
            deps = ["flask", "axios", "pytest", "react-router-dom"]
            _write_node_boilerplate(ws, deps, react_language())
            # package.json must NOT have Python names
            with open(os.path.join(ws, "package.json")) as f:
                pkg = json.load(f)
            self.assertNotIn("flask", pkg["dependencies"])
            self.assertNotIn("pytest", pkg["dependencies"])
            self.assertIn("axios", pkg["dependencies"])
            self.assertIn("react-router-dom", pkg["dependencies"])
            # requirements.txt must have the Python names
            req_path = os.path.join(ws, "requirements.txt")
            self.assertTrue(os.path.exists(req_path))
            with open(req_path) as f:
                req_content = f.read()
            self.assertIn("flask", req_content)
            self.assertIn("pytest", req_content)

    def test_backend_dir_routes_requirements_there(self):
        from cadillac.engine import _write_node_boilerplate
        from cadillac.languages import react_language
        with tempfile.TemporaryDirectory() as ws:
            # Full-stack layout: backend/ already exists (created by
            # scaffold phase or the LLM before boilerplate runs).
            os.makedirs(os.path.join(ws, "backend"))
            _write_node_boilerplate(ws, ["flask", "axios"], react_language())
            # requirements.txt should land INSIDE backend/ where pip expects it
            self.assertTrue(os.path.exists(os.path.join(ws, "backend/requirements.txt")))
            self.assertFalse(os.path.exists(os.path.join(ws, "requirements.txt")))

    def test_no_python_deps_no_requirements(self):
        from cadillac.engine import _write_node_boilerplate
        from cadillac.languages import react_language
        with tempfile.TemporaryDirectory() as ws:
            _write_node_boilerplate(ws, ["react", "axios"], react_language())
            self.assertFalse(os.path.exists(os.path.join(ws, "requirements.txt")))

    def test_react_boilerplate_sets_type_module(self):
        # Prod regression from recipe build #6: without type:module at root,
        # Vite prints "CJS build of Vite's Node API is deprecated" to stderr
        # which validate.py misreads as a build failure. React/Vue/Angular
        # are ESM-native — boilerplate MUST set type:module up front.
        from cadillac.engine import _write_node_boilerplate
        from cadillac.languages import react_language
        with tempfile.TemporaryDirectory() as ws:
            _write_node_boilerplate(ws, ["axios"], react_language())
            with open(os.path.join(ws, "package.json")) as f:
                pkg = json.load(f)
            self.assertEqual(pkg.get("type"), "module")

    def test_add_dep_preserves_type_module_on_vite_project(self):
        # Prod regression from recipe #7: boilerplate correctly set
        # type:module for React but add_dep stripped it on every subsequent
        # package add, re-introducing the CJS deprecation warning.
        from cadillac.tools import ToolExecutor
        from cadillac.manifest import FileManifest
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "package.json"), "w") as f:
                json.dump({
                    "type": "module",
                    "devDependencies": {"vite": "^5", "vitest": "^1"},
                }, f)
            te = ToolExecutor(ws, FileManifest())
            te._config_bypass = True  # bypass path-protection gate for test
            result = te.add_dep("axios", "^1.6", dev=False)
            self.assertEqual(result.get("status"), "ok")
            with open(os.path.join(ws, "package.json")) as f:
                pkg = json.load(f)
            # type:module must survive the add_dep call on Vite projects.
            self.assertEqual(pkg.get("type"), "module")
            self.assertIn("axios", pkg["dependencies"])

    def test_add_dep_strips_type_module_on_cjs_project(self):
        # Original behavior preserved for plain Jest/CJS projects where
        # type:module actually breaks ts-jest.
        from cadillac.tools import ToolExecutor
        from cadillac.manifest import FileManifest
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "package.json"), "w") as f:
                json.dump({
                    "type": "module",  # LLM wrongly added it
                    "devDependencies": {"jest": "^29", "ts-jest": "^29"},
                }, f)
            te = ToolExecutor(ws, FileManifest())
            te._config_bypass = True
            te.add_dep("express", "^4")
            with open(os.path.join(ws, "package.json")) as f:
                pkg = json.load(f)
            self.assertNotIn("type", pkg)

    def test_boilerplate_respects_subdir(self):
        # Full-stack layout: React code is in frontend/, boilerplate must
        # write package.json + index.html THERE, not at workspace root.
        # Otherwise vite's root index.html points to /src/main.tsx which
        # doesn't exist at root (LLM placed it at frontend/src/main.tsx).
        from cadillac.engine import _write_node_boilerplate
        from cadillac.languages import react_language
        with tempfile.TemporaryDirectory() as ws:
            _write_node_boilerplate(ws, ["axios"], react_language(),
                                    subdir="frontend")
            # All boilerplate must be inside frontend/, NOT at workspace root.
            self.assertTrue(os.path.exists(os.path.join(ws, "frontend/package.json")))
            self.assertTrue(os.path.exists(os.path.join(ws, "frontend/tsconfig.json")))
            self.assertTrue(os.path.exists(os.path.join(ws, "frontend/index.html")))
            self.assertTrue(os.path.exists(os.path.join(ws, "frontend/vite.config.ts")))
            self.assertTrue(os.path.exists(os.path.join(ws, "frontend/src/vite-env.d.ts")))
            self.assertFalse(os.path.exists(os.path.join(ws, "package.json")))
            self.assertFalse(os.path.exists(os.path.join(ws, "index.html")))

    def test_cd_to_real_subdir_preserved(self):
        # Prod regression from build #9: `cd frontend && npm install` was
        # getting stripped to just `npm install`, causing ENOENT at workspace
        # root (no root package.json in subdir-layout builds). Real subdirs
        # must be preserved.
        from cadillac.tools import ToolExecutor
        from cadillac.manifest import FileManifest
        from unittest import mock
        with tempfile.TemporaryDirectory() as ws:
            os.makedirs(os.path.join(ws, "frontend"))
            te = ToolExecutor(ws, FileManifest())
            # Mock subprocess so we can inspect the final command.
            with mock.patch("cadillac.tools.subprocess.run") as sp_run:
                class FR: returncode=0; stdout=""; stderr=""
                sp_run.return_value = FR()
                te.run_command("cd frontend && npm install")
                cmd_arg = sp_run.call_args[0][0]
                # The `cd frontend &&` prefix must survive — if stripped,
                # `npm install` at workspace root errors on missing package.json.
                self.assertIn("cd frontend", cmd_arg)
                self.assertIn("npm install", cmd_arg)

    def test_cd_to_nonexistent_still_stripped(self):
        # LLM hallucinations like `cd /testbed && ...` must still get
        # stripped (that's the whole reason the strip exists).
        from cadillac.tools import ToolExecutor
        from cadillac.manifest import FileManifest
        from unittest import mock
        with tempfile.TemporaryDirectory() as ws:
            te = ToolExecutor(ws, FileManifest())
            with mock.patch("cadillac.tools.subprocess.run") as sp_run:
                class FR: returncode=0; stdout=""; stderr=""
                sp_run.return_value = FR()
                te.run_command("cd nonexistent-subdir && ls")
                cmd_arg = sp_run.call_args[0][0]
                self.assertNotIn("cd nonexistent-subdir", cmd_arg)
                self.assertIn("ls", cmd_arg)

    def test_boilerplate_default_writes_to_workspace(self):
        # Back-compat: no subdir → writes at workspace root (legacy behavior).
        from cadillac.engine import _write_node_boilerplate
        from cadillac.languages import react_language
        with tempfile.TemporaryDirectory() as ws:
            _write_node_boilerplate(ws, ["axios"], react_language())
            self.assertTrue(os.path.exists(os.path.join(ws, "package.json")))
            self.assertTrue(os.path.exists(os.path.join(ws, "index.html")))

    def test_typescript_backend_no_type_module(self):
        # Plain TS backend (Express / Node scripts) typically uses Jest/CJS.
        # Preserve the current CJS default — type:module would break ts-jest.
        from cadillac.engine import _write_node_boilerplate
        from cadillac.languages import typescript_language
        with tempfile.TemporaryDirectory() as ws:
            _write_node_boilerplate(ws, ["express"], typescript_language())
            with open(os.path.join(ws, "package.json")) as f:
                pkg = json.load(f)
            self.assertNotIn("type", pkg)


class TestShouldUseModular(unittest.TestCase):
    """Improved heuristic — modular for eBay-scale full-stack, flat for tiny CLIs.

    Prod regression: eBay-scale auction site (25+ files, full-stack
    Python+TS, clean module boundaries) was silently going flat because
    the old heuristic only counted `.py` refs and missed the obvious
    full-stack split.
    """

    def test_simple_cli_stays_flat(self):
        from cadillac.engine import _should_use_modular
        arch = "## Architecture\nSimple CLI: main.py, ops.py, cli.py.\n"
        self.assertFalse(_should_use_modular(arch))

    def test_explicit_modules_section_triggers(self):
        from cadillac.engine import _should_use_modular
        arch = "## Architecture\nstuff\n\n## Modules\n- a: x\n- b: y\n"
        self.assertTrue(_should_use_modular(arch))

    def test_full_stack_split_triggers_even_with_few_files(self):
        # eBay-style: backend + frontend mentioned together → modular
        from cadillac.engine import _should_use_modular
        arch = ("## Architecture\nApp split: backend/ for API, "
                "frontend/ for React. Just a few files.\n")
        self.assertTrue(_should_use_modular(arch))

    def test_many_source_files_across_languages_triggers(self):
        # Old heuristic only counted .py — modern apps have .tsx/.ts/.jsx too
        from cadillac.engine import _should_use_modular
        arch = "\n".join(
            f"file{i}.tsx → does X" for i in range(20)
        )
        self.assertTrue(_should_use_modular(arch))

    def test_four_subdirs_enumerated_triggers(self):
        from cadillac.engine import _should_use_modular
        arch = ("## Architecture\nDirectory layout: auth/, listings/, "
                "bids/, orders/, reviews/, watchlist/")
        self.assertTrue(_should_use_modular(arch))

    def test_common_path_words_dont_count_as_modules(self):
        # `tests/`, `src/`, `node_modules/` shouldn't push us to modular
        from cadillac.engine import _should_use_modular
        arch = ("## Architecture\nSimple project: src/ has main.py, "
                "tests/ has tests, lib/ has helpers, public/ has assets.")
        self.assertFalse(_should_use_modular(arch))

    def test_ebay_architecture_triggers(self):
        # End-to-end: the actual eBay architecture text format that
        # previously went flat must now trigger modular.
        from cadillac.engine import _should_use_modular
        arch = """# eBay-Scale Auction Marketplace — Architecture Document

## Architecture
- Backend: Modular blueprints (auth, listings, bids, watchlist, reviews, orders)
- Frontend: Pages, components, hooks, services
- File Organization:
  - Backend: backend/{app.py,models.py,utils/}, auth/, listings/, bids/, watchlist/, reviews/, orders/
  - Frontend: frontend/src/{main.tsx,App.tsx,pages/,components/,services/,hooks/,types/}

## Interfaces
- backend/models.py: User
- backend/bids/service.py: validate_bid
- frontend/src/services/api.ts: postBid
- frontend/src/components/BidForm.tsx: BidForm
"""
        self.assertTrue(_should_use_modular(arch),
                        msg="full-stack eBay arch should trigger modular pipeline")


if __name__ == "__main__":
    unittest.main()
