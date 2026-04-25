"""Tests for cadillac.dash — data layer only (no TTY / Rich rendering)."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class TestDiscoverWorkspaces(unittest.TestCase):
    def test_sorts_newest_first(self):
        from cadillac.dash import discover_workspaces
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Create 3 workspaces with different mtimes
            names = ["workspace-20260401-000000",
                     "workspace-20260415-000000",
                     "workspace-20260424-000000"]
            for i, n in enumerate(names):
                d = root / n
                d.mkdir()
                # Force mtime: older → earlier timestamp
                ts = time.time() - (len(names) - i) * 3600
                os.utime(d, (ts, ts))
            out = discover_workspaces(root)
            # Newest first → reverse of the ascending mtimes we set
            self.assertEqual([w.name for w in out],
                             ["workspace-20260424-000000",
                              "workspace-20260415-000000",
                              "workspace-20260401-000000"])

    def test_ignores_non_workspace_dirs(self):
        from cadillac.dash import discover_workspaces
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspace-abc").mkdir()
            (root / "somefile.txt").write_text("x")
            (root / "other-dir").mkdir()
            names = [w.name for w in discover_workspaces(root)]
            self.assertEqual(names, ["workspace-abc"])

    def test_empty_root(self):
        from cadillac.dash import discover_workspaces
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(discover_workspaces(Path(tmp)), [])

    def test_nonexistent_root(self):
        from cadillac.dash import discover_workspaces
        self.assertEqual(discover_workspaces(Path("/nonexistent-xyz-123")), [])


class TestParseProgressMd(unittest.TestCase):
    def _sample(self):
        return """# Build Progress

## Task
Build a TS todo list with Jest tests

## Status: COMPLETE (Round 0/10) | Phase 7/7 | Elapsed: 5m 3s

## Plan
- [x] src/index.ts
- [x] src/todo.ts
- [ ] src/routes.ts

## Dependencies
- [x] express
- [x] supertest

## Build Log
- R1: planning
- R2: scaffold
- R3: all files written

## Validation
- [x] Naming
- [x] Imports
- [x] Syntax
- [x] Lint
- [x] Framework
- [x] Functional
- [FAIL] Run
- [FAIL] Tests

## Lessons Applied
- type:module anti-pattern
- vitest v0.34 Node 18 issue
"""

    def test_parses_task(self):
        from cadillac.dash import parse_progress_md
        meta = parse_progress_md(self._sample())
        self.assertIn("TS todo list", meta.task)

    def test_parses_status_line(self):
        from cadillac.dash import parse_progress_md
        meta = parse_progress_md(self._sample())
        self.assertEqual(meta.status, "COMPLETE")
        self.assertIn("Phase", meta.phase_label)
        self.assertIn("5m", meta.elapsed)

    def test_parses_plan_counts(self):
        from cadillac.dash import parse_progress_md
        meta = parse_progress_md(self._sample())
        self.assertEqual(meta.n_files_total, 3)
        self.assertEqual(meta.n_files_done, 2)

    def test_parses_deps_counts(self):
        from cadillac.dash import parse_progress_md
        meta = parse_progress_md(self._sample())
        self.assertEqual(meta.n_deps_total, 2)
        self.assertEqual(meta.n_deps_done, 2)

    def test_parses_validations(self):
        from cadillac.dash import parse_progress_md
        meta = parse_progress_md(self._sample())
        self.assertEqual(meta.validations.get("naming"), "PASS")
        self.assertEqual(meta.validations.get("run"), "FAIL")
        self.assertEqual(meta.validations.get("tests"), "FAIL")

    def test_parses_lessons_applied(self):
        from cadillac.dash import parse_progress_md
        meta = parse_progress_md(self._sample())
        self.assertEqual(len(meta.lessons_applied), 2)
        self.assertIn("type:module", meta.lessons_applied[0])

    def test_parses_build_log_tail(self):
        from cadillac.dash import parse_progress_md
        meta = parse_progress_md(self._sample())
        self.assertEqual(len(meta.build_log_tail), 3)

    def test_partial_progress_md_is_ok(self):
        from cadillac.dash import parse_progress_md
        # Missing sections → empty defaults, not exception
        meta = parse_progress_md("# Build Progress\n\n## Task\nsomething\n")
        self.assertEqual(meta.task, "something")
        self.assertEqual(meta.n_files_total, 0)
        self.assertEqual(meta.validations, {})

    def test_empty_content(self):
        from cadillac.dash import parse_progress_md
        meta = parse_progress_md("")
        self.assertEqual(meta.task, "")


class TestTailBuildJsonl(unittest.TestCase):
    def test_tails_last_n(self):
        from cadillac.dash import tail_build_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "build.jsonl"
            with open(p, "w") as f:
                for i in range(100):
                    f.write(json.dumps({"ts": i, "kind": "log", "msg": f"event-{i}"}) + "\n")
            tail = tail_build_jsonl(p, n=10)
            self.assertEqual(len(tail), 10)
            # Last 10 events = event-90..event-99
            self.assertEqual(tail[-1]["msg"], "event-99")
            self.assertEqual(tail[0]["msg"], "event-90")

    def test_huge_file_does_not_read_all(self):
        # Write a large file; tail_build_jsonl uses seek-to-end so it should
        # skip most of the body.
        from cadillac.dash import TAIL_BYTES, tail_build_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "build.jsonl"
            with open(p, "w") as f:
                # Pad with a long junk line, then 20 real events at end
                long_line = json.dumps({"ts": 0, "kind": "log", "msg": "x" * 200}) + "\n"
                for _ in range(500):  # ~100KB of junk
                    f.write(long_line)
                for i in range(20):
                    f.write(json.dumps({"ts": i, "kind": "log", "msg": f"real-{i}"}) + "\n")
            tail = tail_build_jsonl(p, n=50)
            # Should at least include the real-N events (last TAIL_BYTES worth)
            self.assertTrue(any("real-19" in e.get("msg", "") for e in tail))
            # File is larger than TAIL_BYTES → we can't have read everything
            self.assertGreater(p.stat().st_size, TAIL_BYTES)

    def test_missing_file(self):
        from cadillac.dash import tail_build_jsonl
        self.assertEqual(tail_build_jsonl(Path("/nope.jsonl")), [])

    def test_empty_file(self):
        from cadillac.dash import tail_build_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "empty.jsonl"
            p.write_text("")
            self.assertEqual(tail_build_jsonl(p), [])

    def test_skips_malformed_lines(self):
        from cadillac.dash import tail_build_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.jsonl"
            p.write_text(
                json.dumps({"kind": "a"}) + "\n"
                "not-json\n"
                + json.dumps({"kind": "b"}) + "\n"
            )
            out = tail_build_jsonl(p)
            self.assertEqual([e["kind"] for e in out], ["a", "b"])


class TestLoadWorkspaceMetadata(unittest.TestCase):
    def test_live_build_detection(self):
        from cadillac.dash import LIVE_WINDOW_S, Workspace, load_workspace_metadata
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws_dir = root / "workspace-test"
            ws_dir.mkdir()
            (ws_dir / ".cadillac").mkdir()
            jsonl = ws_dir / ".cadillac" / "build.jsonl"
            jsonl.write_text(json.dumps({"kind": "log"}) + "\n")
            # Fresh mtime → live
            ws = Workspace(path=ws_dir, name=ws_dir.name, mtime=time.time())
            load_workspace_metadata(ws)
            self.assertTrue(ws.meta.is_live)
            # Old mtime → not live
            old = time.time() - LIVE_WINDOW_S - 60
            os.utime(jsonl, (old, old))
            ws2 = Workspace(path=ws_dir, name=ws_dir.name, mtime=time.time())
            load_workspace_metadata(ws2)
            self.assertFalse(ws2.meta.is_live)

    def test_populates_from_progress_md(self):
        from cadillac.dash import Workspace, load_workspace_metadata
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws_dir = root / "workspace-test"
            ws_dir.mkdir()
            (ws_dir / "progress.md").write_text(
                "# Build Progress\n\n## Task\nTest task\n\n"
                "## Status: COMPLETE (Round 0/10) | Phase 7/7 | Elapsed: 1m\n\n"
                "## Plan\n- [x] foo.py\n- [ ] bar.py\n"
            )
            ws = Workspace(path=ws_dir, name=ws_dir.name, mtime=time.time())
            load_workspace_metadata(ws)
            self.assertEqual(ws.meta.task, "Test task")
            self.assertEqual(ws.meta.status, "COMPLETE")
            self.assertEqual(ws.meta.n_files_total, 2)
            self.assertEqual(ws.meta.n_files_done, 1)

    def test_plan_json_extracts_modular_flag(self):
        from cadillac.dash import Workspace, load_workspace_metadata
        with tempfile.TemporaryDirectory() as tmp:
            ws_dir = Path(tmp) / "workspace-test"
            ws_dir.mkdir()
            (ws_dir / "plan.json").write_text(json.dumps({
                "entry_point": "main.py", "modular": True, "files": [],
            }))
            ws = Workspace(path=ws_dir, name=ws_dir.name, mtime=time.time())
            load_workspace_metadata(ws)
            self.assertEqual(ws.meta.plan_entry_point, "main.py")
            self.assertTrue(ws.meta.plan_modular)


class TestAggregators(unittest.TestCase):
    def test_memory_aggregate_handles_empty(self):
        from cadillac import dash
        with mock.patch("cadillac.memory.load_lessons", return_value=[]):
            out = dash.aggregate_memory()
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["top"], [])

    def test_memory_aggregate_summarizes(self):
        from cadillac import dash
        from cadillac.memory import Lesson
        lessons = [
            Lesson(ts=time.time(), type="error_pattern",
                   trigger="t1", fix="f1", confidence=0.9, used=3,
                   polarity="do", tags=["python", "flask"]),
            Lesson(ts=time.time(), type="error_pattern",
                   trigger="t2", fix="f2", confidence=0.5, used=1,
                   polarity="dont", tags=["python"]),
            Lesson(ts=time.time(), type="error_pattern",
                   trigger="t3", fix="f3", confidence=0.7, used=2,
                   polarity="do", tags=["typescript", "react"]),
        ]
        with mock.patch("cadillac.memory.load_lessons", return_value=lessons):
            out = dash.aggregate_memory()
        self.assertEqual(out["total"], 3)
        # Sorted by confidence desc → t1, t3, t2
        self.assertEqual(out["top"][0].trigger, "t1")
        self.assertEqual(out["by_polarity"]["do"], 2)
        self.assertEqual(out["by_polarity"]["dont"], 1)
        self.assertEqual(out["by_tag"]["python"], 2)
        self.assertEqual(out["by_tag"]["flask"], 1)

    def test_phase_budgets_aggregate(self):
        from cadillac import dash
        rows = [
            {"phase": "build", "rounds": 10, "tags": ["python"]},
            {"phase": "build", "rounds": 20, "tags": ["python"]},
            {"phase": "build", "rounds": 30, "tags": ["python"]},
            {"phase": "build", "rounds": 5,  "tags": ["typescript"]},
            {"phase": "scaffold", "rounds": 7, "tags": ["python"]},
        ]
        with mock.patch("cadillac.memory._load_phase_history", return_value=rows):
            out = dash.aggregate_phase_budgets()
        self.assertEqual(out["total_rows"], 5)
        python_build = out["by_tag_phase"][("python", "build")]
        self.assertEqual(python_build["count"], 3)
        self.assertEqual(python_build["mean"], 20.0)
        self.assertEqual(python_build["max"], 30)


class TestStatusBadgeFormatting(unittest.TestCase):
    def test_live_overrides(self):
        from cadillac.dash import _format_status_badge
        out = _format_status_badge("BUILD", is_live=True)
        self.assertIn("LIVE", out)

    def test_complete_is_green(self):
        from cadillac.dash import _format_status_badge
        out = _format_status_badge("COMPLETE", is_live=False)
        self.assertIn("green", out)

    def test_stopped_is_red(self):
        from cadillac.dash import _format_status_badge
        out = _format_status_badge("STOPPED", is_live=False)
        self.assertIn("red", out)

    def test_unknown_status(self):
        from cadillac.dash import _format_status_badge
        out = _format_status_badge("", is_live=False)
        self.assertIn("?", out)


class TestValidationFormatting(unittest.TestCase):
    def test_empty_shows_not_run(self):
        from cadillac.dash import _format_validations
        self.assertIn("not yet run", _format_validations({}))

    def test_pass_fail_colored(self):
        from cadillac.dash import _format_validations
        out = _format_validations({"naming": "PASS", "tests": "FAIL"})
        self.assertIn("nam:✓", out)
        self.assertIn("tes:✗", out)


class TestHandleKeyNavigation(unittest.TestCase):
    def test_up_down_moves_selection(self):
        from cadillac.dash import DashState, Workspace, _handle_key
        state = DashState()
        state.workspaces = [
            Workspace(path=Path("/ws-1"), name="ws-1", mtime=1.0),
            Workspace(path=Path("/ws-2"), name="ws-2", mtime=2.0),
            Workspace(path=Path("/ws-3"), name="ws-3", mtime=3.0),
        ]
        state.selected_idx = 1
        _handle_key("down", state)
        self.assertEqual(state.selected_idx, 2)
        _handle_key("down", state)  # clamps at end
        self.assertEqual(state.selected_idx, 2)
        _handle_key("up", state)
        self.assertEqual(state.selected_idx, 1)

    def test_q_exits(self):
        from cadillac.dash import DashState, _handle_key
        state = DashState()
        self.assertFalse(_handle_key("q", state))

    def test_view_switch(self):
        from cadillac.dash import DashState, _handle_key
        state = DashState()
        _handle_key("m", state)
        self.assertEqual(state.view, "memory")
        _handle_key("b", state)
        self.assertEqual(state.view, "budgets")
        _handle_key("esc", state)
        self.assertEqual(state.view, "projects")

    def test_filter_typing(self):
        from cadillac.dash import DashState, _handle_key
        state = DashState()
        _handle_key("/", state)
        self.assertTrue(state.filter_mode)
        _handle_key("p", state)
        _handle_key("y", state)
        self.assertEqual(state.filter_text, "py")
        _handle_key("backspace", state)
        self.assertEqual(state.filter_text, "p")
        _handle_key("enter", state)  # commit
        self.assertFalse(state.filter_mode)


class TestDashStateFiltering(unittest.TestCase):
    def test_filter_by_name(self):
        from cadillac.dash import DashState, Workspace, WorkspaceMeta
        state = DashState()
        state.workspaces = [
            Workspace(path=Path("/a"), name="workspace-pygame", mtime=1.0,
                      meta=WorkspaceMeta(task="a pygame build")),
            Workspace(path=Path("/b"), name="workspace-flask", mtime=2.0,
                      meta=WorkspaceMeta(task="a flask build")),
        ]
        state.filter_text = "pygame"
        filtered = state.filtered
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].name, "workspace-pygame")

    def test_filter_by_task_text(self):
        from cadillac.dash import DashState, Workspace, WorkspaceMeta
        state = DashState()
        state.workspaces = [
            Workspace(path=Path("/a"), name="ws-a", mtime=1.0,
                      meta=WorkspaceMeta(task="do a flask thing")),
            Workspace(path=Path("/b"), name="ws-b", mtime=2.0,
                      meta=WorkspaceMeta(task="do a pygame thing")),
        ]
        state.filter_text = "pygame"
        filtered = state.filtered
        self.assertEqual(len(filtered), 1)
        self.assertIn("pygame", filtered[0].meta.task)


if __name__ == "__main__":
    unittest.main()
