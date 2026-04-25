"""Tests for validate.check_static_names + validate.check_smoke_run.

Both checks target the recurring 'NameError in untested-paths' bug class:
the syntax check passes (parseable), the imports check passes (declared
imports resolve), the run check passes (--test mode short-circuits to
pure-logic tests), and the build ships green but crashes when actually
launched.

static_names runs pyflakes to catch the static cases (missing imports,
undefined names) before the runtime path executes them.

smoke_run exercises the no-args interactive entry path with a fake stdscr
so the curses/pygame loop is actually walked, catching the runtime cases
static analysis can't reach (signature mismatches, free-variable lookups).
"""

import os
import tempfile
import unittest
from pathlib import Path


class TestCheckStaticNames(unittest.TestCase):

    def setUp(self):
        from cadillac.languages import python_language
        self.lang = python_language()

    def _ws(self, files: dict) -> str:
        """Make a temp workspace with the given {relpath: content} files."""
        d = tempfile.mkdtemp()
        for path, content in files.items():
            full = os.path.join(d, path)
            os.makedirs(os.path.dirname(full) or d, exist_ok=True)
            with open(full, "w") as f:
                f.write(content)
        return d

    def test_clean_file_passes(self):
        from cadillac.validate import check_static_names
        ws = self._ws({
            "main.py": "import os\n\ndef hello():\n    return os.environ.get('USER')\n",
        })
        results = check_static_names(ws, self.lang)
        self.assertTrue(results[0].passed, msg=results[0].output)

    def test_undefined_name_caught(self):
        # The classic Galaga bug: reference Optional + InputPoller without import
        from cadillac.validate import check_static_names
        ws = self._ws({
            "main.py": (
                "from typing import Dict\n"
                "\n"
                "class Foo:\n"
                "    def run(self):\n"
                "        x: Optional[int] = None\n"
                "        poller = InputPoller(None)\n"
                "        return x, poller\n"
            ),
        })
        results = check_static_names(ws, self.lang)
        self.assertFalse(results[0].passed)
        self.assertIn("Optional", results[0].output)
        self.assertIn("InputPoller", results[0].output)

    def test_module_referenced_without_import(self):
        # input_poller.py:73 — `curses.KEY_LEFT` used without `import curses`
        from cadillac.validate import check_static_names
        ws = self._ws({
            "main.py": (
                "def poll(key):\n"
                "    return key in (ord('a'), curses.KEY_LEFT)\n"
            ),
        })
        results = check_static_names(ws, self.lang)
        self.assertFalse(results[0].passed)
        self.assertIn("curses", results[0].output)

    def test_unused_import_does_not_fail(self):
        # Style noise — `lint` handles it, not `static_names`.
        from cadillac.validate import check_static_names
        ws = self._ws({
            "main.py": "import os\nimport sys\n\nprint(os.getcwd())\n",
        })
        results = check_static_names(ws, self.lang)
        self.assertTrue(results[0].passed,
            msg=f"unused 'sys' import should not fail static_names: {results[0].output}")

    def test_skips_node_projects(self):
        from cadillac.validate import check_static_names
        from cadillac.languages import typescript_language
        ws = self._ws({"src/main.ts": "const x: any = undefined;\n"})
        results = check_static_names(ws, typescript_language())
        self.assertTrue(results[0].passed)
        self.assertIn("Python-only", results[0].output)

    def test_skips_pycache_and_venv(self):
        from cadillac.validate import check_static_names
        ws = self._ws({
            "main.py": "x = 1\n",
            "__pycache__/main.cpython-312.pyc": "garbage",
            ".venv/lib/python3.12/site-packages/foo.py": "x = undefined_name\n",
        })
        results = check_static_names(ws, self.lang)
        self.assertTrue(results[0].passed,
            msg=f"should skip __pycache__/.venv: {results[0].output}")


class TestCheckSmokeRun(unittest.TestCase):

    def setUp(self):
        from cadillac.languages import python_language
        self.lang = python_language()

    def _ws(self, files: dict) -> str:
        d = tempfile.mkdtemp()
        for path, content in files.items():
            full = os.path.join(d, path)
            os.makedirs(os.path.dirname(full) or d, exist_ok=True)
            with open(full, "w") as f:
                f.write(content)
        return d

    def test_skips_when_no_curses_or_pygame(self):
        from cadillac.validate import check_smoke_run
        ws = self._ws({"main.py": "print('hello')\n"})
        results = check_smoke_run(ws, self.lang)
        self.assertTrue(results[0].passed)
        self.assertIn("No curses/pygame", results[0].output)

    def test_clean_curses_app_passes(self):
        from cadillac.validate import check_smoke_run
        ws = self._ws({
            "main.py": (
                "import curses\n"
                "\n"
                "def game(stdscr):\n"
                "    stdscr.clear()\n"
                "    stdscr.refresh()\n"
                "    while stdscr.getch() != ord('q'):\n"
                "        stdscr.refresh()\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    curses.wrapper(game)\n"
            ),
        })
        results = check_smoke_run(ws, self.lang)
        self.assertTrue(results[0].passed,
            msg=f"clean curses app should pass smoke: {results[0].output[:300]}")
        self.assertIn("frames clean", results[0].output)

    def test_curses_with_runtime_nameerror_fails(self):
        # Like Galaga's input_poller — references curses.KEY_LEFT without import.
        # Static check would catch it; smoke ensures we'd catch even runtime-only.
        from cadillac.validate import check_smoke_run
        ws = self._ws({
            "main.py": (
                "import curses\n"
                "\n"
                "def game(stdscr):\n"
                "    while True:\n"
                "        key = stdscr.getch()\n"
                "        # Reference an undefined symbol at runtime\n"
                "        if key == nonexistent_symbol_xyz:\n"
                "            break\n"
                "        stdscr.refresh()\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    curses.wrapper(game)\n"
            ),
        })
        results = check_smoke_run(ws, self.lang)
        self.assertFalse(results[0].passed)
        self.assertIn("nonexistent_symbol_xyz", results[0].output.lower()
                      .replace("nonexistent", "nonexistent"))

    def test_curses_imported_but_wrapper_never_called_fails(self):
        # The Galaga main.py bug: curses is imported but the __main__ block
        # routes to a broken cli() that never calls curses.wrapper.
        from cadillac.validate import check_smoke_run
        ws = self._ws({
            "main.py": (
                "import curses\n"
                "\n"
                "def game(stdscr):\n"
                "    pass\n"
                "\n"
                "def broken_cli():\n"
                "    # Silently exits without ever calling curses.wrapper\n"
                "    pass\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    broken_cli()\n"
            ),
        })
        results = check_smoke_run(ws, self.lang)
        self.assertFalse(results[0].passed)
        self.assertIn("never called curses.wrapper", results[0].output)

    def test_no_main_py_skips(self):
        from cadillac.validate import check_smoke_run
        ws = self._ws({"app.py": "import curses\n"})
        results = check_smoke_run(ws, self.lang)
        self.assertTrue(results[0].passed)
        self.assertIn("No main.py", results[0].output)


class TestRunValidationIncludesNewChecks(unittest.TestCase):
    """Guard against dropping the new checks from the pipeline."""

    def test_run_validation_calls_static_names(self):
        import inspect
        from cadillac.validate import run_validation
        src = inspect.getsource(run_validation)
        self.assertIn("check_static_names", src)
        self.assertIn("check_smoke_run", src)

    def test_results_to_dict_includes_both(self):
        import inspect
        from cadillac.validate import results_to_dict
        src = inspect.getsource(results_to_dict)
        self.assertIn('"static_names"', src)
        self.assertIn('"smoke_run"', src)


if __name__ == "__main__":
    unittest.main()
