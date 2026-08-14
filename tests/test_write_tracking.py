"""Tests for write-tracking accuracy and terminal-abort teardown.

Two bugs found by the 2026-08-13 validation builds:

1. `file_written` / `progress.mark_file_done()` / `batch_tracker.file_written()`
   fired off the tool call's ARGUMENTS, never its result. A write that failed
   still marked the file done, which fed premature
   "[ALL BATCHES COMPLETE] All planned files written" and a progress view that
   disagreed with the disk. Seen live as a failed `write_file(main.py)`
   immediately followed by a `[+] main.py` line.

2. After `run()` aborted on a terminal provider failure, `build()` walked on
   into auto-iterate and burned another doomed LLM call before the error
   surfaced.
"""

import unittest
from unittest import mock

from cadillac.engine import ProviderFailure, QuotaExhausted, _write_succeeded


class TestWriteSucceeded(unittest.TestCase):
    """The predicate that decides whether a write really landed."""

    def test_ok_status_is_success(self):
        self.assertTrue(_write_succeeded({"status": "ok", "path": "a.py", "lines": 3}))

    def test_error_key_is_failure(self):
        self.assertFalse(_write_succeeded({"error": "Path escapes workspace"}))

    def test_schema_validation_error_is_failure(self):
        """The exact shape the new arg validator returns."""
        self.assertFalse(_write_succeeded(
            {"error": "write_file: missing required parameter(s) 'content'."}
        ))

    def test_protected_config_block_is_failure(self):
        self.assertFalse(_write_succeeded({"error": "BLOCKED: tsconfig.json is protected"}))

    def test_missing_status_is_failure(self):
        """A dict with neither status nor error is not a confirmed write."""
        self.assertFalse(_write_succeeded({"path": "a.py"}))

    def test_none_is_failure(self):
        """An unfilled batch slot (tool never ran) must not count."""
        self.assertFalse(_write_succeeded(None))

    def test_non_dict_is_failure(self):
        self.assertFalse(_write_succeeded("ok"))
        self.assertFalse(_write_succeeded(["ok"]))

    def test_error_wins_over_status(self):
        self.assertFalse(_write_succeeded({"status": "ok", "error": "but actually failed"}))


class TestWriteTrackingIsGated(unittest.TestCase):
    """Pin that the engine actually consults the result, not just the args."""

    def _flush_batch_source(self) -> str:
        import inspect

        from cadillac.engine import process_tool_calls
        src = inspect.getsource(process_tool_calls)
        start = src.index("def flush_batch")
        end = src.index("for tc, fn_name, fn_args in parsed", start)
        return src[start:end]

    def test_results_are_collected(self):
        self.assertIn("results", self._flush_batch_source())

    def test_emit_is_behind_the_success_gate(self):
        body = self._flush_batch_source()
        gate = body.index("_write_succeeded")
        emit = body.index('emit("file_written"')
        self.assertLess(gate, emit,
                        "file_written must be emitted only after the success check")

    def test_progress_and_tracker_are_behind_the_gate(self):
        body = self._flush_batch_source()
        gate = body.index("_write_succeeded")
        for call in ("progress.mark_file_done", "batch_tracker.file_written"):
            self.assertLess(gate, body.index(call),
                            f"{call} must be behind the success check")


class TestTerminalAbortStopsBuild(unittest.TestCase):
    """A terminal provider failure must not lead to further model calls."""

    def _run_build(self, exc):
        import cadillac.engine as engine

        calls = {"iterate": 0, "package": 0, "reflection": 0}

        def fake_run(*a, **k):
            raise exc

        with mock.patch.object(engine, "run", side_effect=fake_run), \
             mock.patch.object(engine, "iterate",
                               side_effect=lambda *a, **k: calls.__setitem__("iterate", calls["iterate"] + 1)), \
             mock.patch.object(engine, "_write_package_files",
                               side_effect=lambda *a, **k: calls.__setitem__("package", calls["package"] + 1)), \
             mock.patch.object(engine, "_run_reflection",
                               side_effect=lambda *a, **k: calls.__setitem__("reflection", calls["reflection"] + 1)), \
             mock.patch.object(engine, "_load_workspace", return_value=(None, mock.Mock(files={}))), \
             mock.patch.object(engine, "detect_language", return_value=mock.Mock(entry_point="main.py")):
            with self.assertRaises(ProviderFailure):
                engine.build("t", "/tmp/nonexistent-ws", mock.Mock())
        return calls

    def test_iterate_is_not_called(self):
        """The wasted LLM call: build() used to walk into auto-iterate."""
        calls = self._run_build(QuotaExhausted("quota gone"))
        self.assertEqual(calls["iterate"], 0)

    def test_reflection_is_not_called(self):
        calls = self._run_build(QuotaExhausted("quota gone"))
        self.assertEqual(calls["reflection"], 0)

    def test_partial_work_is_still_packaged(self):
        """Packaging is deterministic and needs no provider, so partial work
        must still get its README/.gitignore."""
        calls = self._run_build(QuotaExhausted("quota gone"))
        self.assertEqual(calls["package"], 1)

    def test_failure_propagates_for_nonzero_exit(self):
        """_run_build already asserts the raise; this pins that the generic
        ProviderFailure base is handled, not just the named subclasses."""
        calls = self._run_build(ProviderFailure("endpoint down"))
        self.assertEqual(calls["iterate"], 0)
        self.assertEqual(calls["package"], 1)


class TestTerminalHandlersReraise(unittest.TestCase):
    """Every terminal provider handler in run() must re-raise after teardown."""

    def test_no_handler_swallows_a_provider_failure(self):
        import inspect
        import re

        from cadillac.engine import run
        src = inspect.getsource(run)
        for marker in ("ABORT endpoint_down", "ABORT endpoint_misconfigured",
                       "ABORT quota_exhausted", "ABORT empty_response",
                       "ABORT provider_failure"):
            i = src.index(marker)
            window = src[i:i + 900]
            m = re.search(r"_interrupted_for_reraise = (\w+)", window)
            self.assertIsNotNone(m, f"{marker}: no re-raise assignment found")
            self.assertEqual(m.group(1), "e",
                             f"{marker} swallows the failure instead of re-raising")


if __name__ == "__main__":
    unittest.main()
