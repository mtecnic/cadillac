"""Tests for the two fixes with the strongest multi-build evidence.

1. MODULE CWD MISCONCEPTION. Write paths are module-relative (`write_file`
   auto-prefixes a bare name with module_path) but `run_command` deliberately
   runs at the WORKSPACE ROOT so pytest and cross-module imports resolve.
   Neither module prompt ever said so, so the model reliably concluded it was
   "inside" the module directory and reached for `../`. Observed across four
   real builds: `ls ../tests/`, `mv x ../tests/core/x.py`,
   `mkdir -p ../tests/core`, `cat ../plan.json`. The command policy denied them
   correctly — two would have written into the workspace parent — but a bare
   denial doesn't correct the belief, so the model tried a variation next round.

2. ENTRY STUB IN A PACKAGE __init__.py. `_write_python_entry_stub` stamped an
   argv/`main()` CLI template at plan.entry_point. When the entry point IS a
   package `__init__.py` (a library), that told the build its public API surface
   was a runnable script — and left an `__init__` with a main() and no
   re-exports, which the library-surface detector cannot recognise. A real
   build's own reflection independently produced "relying on main.py as the
   entry point for a library" as an anti-pattern it had to discover the hard way.
"""

import os
import tempfile
import unittest

from cadillac.manifest import FileManifest
from cadillac.tools import ModuleScopedExecutor


class TestModuleShellCwdIsExplained(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = self._td.name
        os.makedirs(os.path.join(self.ws, "core"), exist_ok=True)
        os.makedirs(os.path.join(self.ws, "tests"), exist_ok=True)
        self.ex = ModuleScopedExecutor(self.ws, FileManifest(), "core")
        self.ex.config_writes_allowed = True

    def tearDown(self):
        self._td.cleanup()

    def test_escape_denial_carries_a_cwd_hint(self):
        result = self.ex.run_command("ls ../tests/")
        self.assertIn("error", result)
        self.assertIn("hint", result)
        self.assertIn("WORKSPACE ROOT", result["hint"])

    def test_hint_names_the_module_path(self):
        result = self.ex.run_command("mkdir -p ../tests/core")
        self.assertIn("core", result.get("hint", ""))

    def test_every_observed_escape_shape_is_corrected(self):
        """The exact commands seen in real builds."""
        for cmd in ("ls ../tests/", "mv core/x.py ../tests/core/x.py",
                    "mkdir -p ../tests/core", "cat ../plan.json",
                    "find .. -name '*.py'"):
            result = self.ex.run_command(cmd)
            self.assertIn("error", result, cmd)
            self.assertIn("hint", result, f"no cwd correction for: {cmd}")

    def test_ordinary_command_gets_no_hint(self):
        """The hint must only appear when it is the actual problem."""
        result = self.ex.run_command("ls core")
        self.assertNotIn("hint", result)

    def test_non_escape_denial_gets_no_cwd_hint(self):
        """A credential denial is not a cwd misunderstanding."""
        result = self.ex.run_command("cat ~/.ssh/id_rsa")
        self.assertIn("error", result)
        self.assertNotIn("hint", result)

    def test_workspace_relative_path_still_works(self):
        """The fix must not change what actually runs."""
        result = self.ex.run_command("ls core tests")
        self.assertEqual(result.get("exit_code"), 0, result)


class TestModulePromptsStateTheCwd(unittest.TestCase):
    def test_scaffold_prompt_states_where_the_shell_runs(self):
        from cadillac.prompts import build_module_scaffold_prompt
        import inspect

        sig = inspect.signature(build_module_scaffold_prompt)
        kwargs = {n: "" for n in sig.parameters if n != "lang"}
        kwargs.update(module_name="core", module_path="core/")
        text = build_module_scaffold_prompt(**kwargs)
        self.assertIn("WORKSPACE ROOT", text)
        self.assertIn("run_command", text)

    def test_build_prompt_states_where_the_shell_runs(self):
        from cadillac.prompts import build_module_build_prompt

        text = build_module_build_prompt(module_name="core", module_path="core")
        self.assertIn("WORKSPACE ROOT", text)

    def test_build_prompt_defaults_module_path_to_name(self):
        """Callers that omit module_path must still get concrete guidance."""
        from cadillac.prompts import build_module_build_prompt

        text = build_module_build_prompt(module_name="core")
        self.assertIn("core/", text)

    def test_build_prompt_accepts_module_path_kwarg(self):
        """Both engine call sites pass it; the signature must accept it."""
        import inspect

        from cadillac.prompts import build_module_build_prompt
        self.assertIn("module_path",
                      inspect.signature(build_module_build_prompt).parameters)

    def test_engine_passes_module_path_at_every_call_site(self):
        import inspect

        from cadillac.engine import _build_module, _iterate_module
        for fn in (_build_module, _iterate_module):
            src = inspect.getsource(fn)
            if "build_module_build_prompt(" not in src:
                continue
            i = src.index("build_module_build_prompt(")
            call = src[i:i + 500]
            self.assertIn("module_path=", call,
                          f"{fn.__name__} does not pass module_path")


class TestEntryStubRespectsLibraries(unittest.TestCase):
    def _stub(self, entry_path):
        from cadillac.engine import _write_python_entry_stub

        with tempfile.TemporaryDirectory() as td:
            wrote = _write_python_entry_stub(td, entry_path)
            self.assertTrue(wrote)
            with open(os.path.join(td, entry_path)) as f:
                return f.read()

    def test_package_init_gets_an_api_surface_not_a_cli(self):
        body = self._stub("ctxpack/__init__.py")
        self.assertIn("__all__", body)
        self.assertNotIn("def main(", body)
        self.assertNotIn("sys.argv", body)

    def test_script_entry_still_gets_the_cli_template(self):
        body = self._stub("main.py")
        self.assertIn("def main(", body)
        self.assertIn("sys.argv", body)

    def test_nested_script_entry_still_gets_the_cli_template(self):
        body = self._stub("cli/run.py")
        self.assertIn("def main(", body)

    def test_library_stub_is_recognised_as_a_library_surface(self):
        """The whole point: the stub must not make the project undetectable."""
        from cadillac.engine import _write_python_entry_stub
        from cadillac.languages import python_language
        from cadillac.runtime import _has_library_surface

        with tempfile.TemporaryDirectory() as td:
            _write_python_entry_stub(td, "ctxpack/__init__.py")
            # A library re-exports something; the stub gives it the shape to.
            with open(os.path.join(td, "ctxpack", "__init__.py"), "a") as f:
                f.write("from .packer import pack\n")
            os.makedirs(os.path.join(td, "ctxpack"), exist_ok=True)
            with open(os.path.join(td, "ctxpack", "packer.py"), "w") as f:
                f.write("def pack(): pass\n")
            self.assertTrue(_has_library_surface(td, python_language()))

    def test_existing_file_is_never_overwritten(self):
        from cadillac.engine import _write_python_entry_stub

        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "pkg"))
            path = os.path.join(td, "pkg", "__init__.py")
            with open(path, "w") as f:
                f.write("REAL = 1\n")
            self.assertFalse(_write_python_entry_stub(td, "pkg/__init__.py"))
            with open(path) as f:
                self.assertEqual(f.read(), "REAL = 1\n")


if __name__ == "__main__":
    unittest.main()
