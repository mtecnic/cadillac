"""Tests for argument validation at the tool dispatch boundary.

`dispatch` used to be `method(**fn_args)` inside a bare try, so one hallucinated
key produced a Python traceback string:

    Tool error (TypeError): write_file() got an unexpected keyword argument
    'filename'

...which never names the parameter the model should have used. Measured cost
was ~13 wasted round-trips per build. The validator turns that into either a
silent correction (near-miss rename, extra key dropped) or one actionable
sentence listing the accepted parameters.

This layer is fail-SAFE, not fail-closed: it exists to save round-trips. The
real security boundaries (path confinement, command policy, config guard) live
inside the tool methods and run after it — see test_command_policy.py.
"""

import os
import tempfile
import unittest

from cadillac.manifest import FileManifest
from cadillac.tools import TOOL_DEFS, TOOL_SCHEMAS, ToolExecutor, validate_tool_args


class _ExecCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = self._td.name
        self.ex = ToolExecutor(self.ws, FileManifest())
        self.ex.config_writes_allowed = True

    def tearDown(self):
        self._td.cleanup()


class TestSchemaRegistry(unittest.TestCase):
    def test_every_tool_has_a_schema(self):
        for d in TOOL_DEFS:
            self.assertIn(d["function"]["name"], TOOL_SCHEMAS)

    def test_schema_names_match_executor_methods(self):
        """A published tool with no implementation would 404 at runtime."""
        for name in TOOL_SCHEMAS:
            self.assertTrue(
                callable(getattr(ToolExecutor, name, None)),
                f"tool '{name}' is published in TOOL_DEFS but not implemented",
            )

    def test_declared_params_are_accepted_by_the_method(self):
        """The regression that costs round-trips: the schema tells the model to
        send a key the implementation would reject with a TypeError."""
        import inspect

        for name, schema in TOOL_SCHEMAS.items():
            method = getattr(ToolExecutor, name)
            params = inspect.signature(method).parameters
            takes_kwargs = any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            if takes_kwargs:
                continue
            for declared in (schema.get("properties") or {}):
                self.assertIn(
                    declared, params,
                    f"{name} schema declares '{declared}' but the method does not accept it",
                )


class TestMissingRequired(_ExecCase):
    def test_missing_required_names_it(self):
        result = self.ex.dispatch("write_file", {"path": "a.py"})
        self.assertIn("error", result)
        self.assertIn("content", result["error"])

    def test_missing_required_lists_accepted_params(self):
        result = self.ex.dispatch("write_file", {})
        self.assertIn("Accepted parameters", result["error"])
        self.assertIn("path", result["error"])
        self.assertIn("content", result["error"])

    def test_error_is_not_a_python_traceback(self):
        """The whole point — no TypeError prose leaking to the model."""
        result = self.ex.dispatch("write_file", {"path": "a.py"})
        self.assertNotIn("TypeError", result["error"])
        self.assertNotIn("unexpected keyword", result["error"])

    def test_missing_multiple_required(self):
        result = self.ex.dispatch("line_edit", {"path": "a.py"})
        for name in ("start_line", "end_line", "new_content"):
            self.assertIn(name, result["error"])


class TestUnknownKeys(_ExecCase):
    def test_near_miss_is_renamed_and_call_succeeds(self):
        """'contents' -> 'content' is unambiguous; don't burn a round-trip."""
        result = self.ex.dispatch("write_file", {"path": "a.py", "contents": "x = 1"})
        self.assertNotIn("error", result)
        self.assertTrue(os.path.exists(os.path.join(self.ws, "a.py")))
        self.assertTrue(any("renamed" in n for n in result.get("arg_warnings", [])))

    def test_unrelated_key_dropped_and_call_succeeds(self):
        result = self.ex.dispatch(
            "write_file", {"path": "a.py", "content": "x = 1", "encoding": "utf-8"}
        )
        self.assertNotIn("error", result)
        self.assertTrue(any("encoding" in n for n in result.get("arg_warnings", [])))

    def test_dropped_key_does_not_reach_the_method(self):
        result = self.ex.dispatch(
            "write_file", {"path": "a.py", "content": "x", "bogus": 1}
        )
        self.assertNotIn("error", result)

    def test_undeclared_but_implemented_alias_still_works(self):
        """read_file accepts offset/limit/start_line beyond its schema; the
        validator must not reject what the implementation supports."""
        self.ex.dispatch("write_file", {"path": "a.py", "content": "1\n2\n3\n4\n"})
        for kwargs in ({"start_line": 2, "end_line": 3}, {"line_start": 2, "line_end": 3}):
            result = self.ex.dispatch("read_file", {"path": "a.py", **kwargs})
            self.assertNotIn("error", result, kwargs)

    def test_non_string_key_dropped(self):
        cleaned, error, notes = validate_tool_args(
            "write_file", {"path": "a.py", "content": "x", 3: "y"},
            getattr(self.ex, "write_file"),
        )
        self.assertIsNone(error)
        self.assertNotIn(3, cleaned)


class TestTypeCoercion(_ExecCase):
    def test_string_int_coerced_for_integer_param(self):
        self.ex.dispatch("write_file", {"path": "a.py", "content": "1\n2\n3\n"})
        result = self.ex.dispatch(
            "line_edit",
            {"path": "a.py", "start_line": "1", "end_line": "2", "new_content": "X"},
        )
        self.assertNotIn("error", result)

    def test_number_coerced_to_string_param(self):
        cleaned, error, _ = validate_tool_args(
            "note_lesson", {"category": "reminder", "content": 42},
            getattr(self.ex, "note_lesson"),
        )
        self.assertIsNone(error)
        self.assertEqual(cleaned["content"], "42")

    def test_bool_string_coerced(self):
        cleaned, error, _ = validate_tool_args(
            "add_dep", {"name": "zod", "dev": "true"}, getattr(self.ex, "add_dep")
        )
        self.assertIsNone(error)
        self.assertIs(cleaned["dev"], True)

    def test_required_wrong_shape_is_an_error(self):
        result = self.ex.dispatch("edit_file", {"path": "a.py", "edits": "not-a-list"})
        self.assertIn("error", result)
        self.assertIn("edits", result["error"])
        self.assertIn("array", result["error"])

    def test_optional_wrong_shape_is_dropped_not_fatal(self):
        cleaned, error, notes = validate_tool_args(
            "search_files", {"pattern": "TODO", "file_glob": ["a", "b"]},
            getattr(self.ex, "search_files"),
        )
        self.assertIsNone(error)
        self.assertNotIn("file_glob", cleaned)


class TestUnknownTool(_ExecCase):
    def test_unknown_tool_lists_available(self):
        result = self.ex.dispatch("frobnicate", {"x": 1})
        self.assertIn("error", result)
        self.assertIn("write_file", result["error"])

    def test_private_attribute_is_not_dispatchable(self):
        """`getattr(self, name)` would happily hand back internals."""
        result = self.ex.dispatch("_check_path", {"path": "a.py"})
        self.assertIn("error", result)
        self.assertIn("Unknown tool", result["error"])

    def test_non_dict_args_rejected_clearly(self):
        result = self.ex.dispatch("write_file", ["a.py", "x"])
        self.assertIn("error", result)
        self.assertIn("JSON object", result["error"])


class TestHappyPathUnchanged(_ExecCase):
    """Well-formed calls must behave exactly as before, with no extra keys."""

    def test_write_succeeds_cleanly(self):
        w = self.ex.dispatch("write_file", {"path": "a.py", "content": "x = 1\n"})
        self.assertEqual(w.get("status"), "ok")
        self.assertNotIn("arg_warnings", w)

    def test_read_returns_content(self):
        # Written on disk directly: read_file deliberately withholds content
        # for files the agent itself wrote (it returns a "you wrote this"
        # note instead, to save context), so a round-trip through write_file
        # would not exercise the read path.
        with open(os.path.join(self.ws, "external.py"), "w") as f:
            f.write("x = 1\n")
        r = self.ex.dispatch("read_file", {"path": "external.py"})
        self.assertNotIn("error", r)
        self.assertIn("x = 1", r.get("content", ""))

    def test_no_arg_tool(self):
        result = self.ex.dispatch("check_status", {})
        self.assertNotIn("error", result)

    def test_tool_level_error_still_surfaces(self):
        """Validation must not mask a genuine tool failure."""
        result = self.ex.dispatch("read_file", {"path": "does-not-exist.py"})
        self.assertIn("error", result)

    def test_security_boundary_still_runs_after_validation(self):
        """The validator is fail-safe; the real gate is inside the method."""
        result = self.ex.dispatch("write_file", {"path": "../escape.py", "content": "x"})
        self.assertIn("error", result)
        self.assertIn("escape", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
