"""Tests for the runtime strategy-dispatch chain (cadillac/runtime/__init__.py).

Each test stages a synthetic workspace with the shape of one matrix task
and asserts `_pick_strategy` returns the right strategy + reason.
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest

from cadillac.languages import python_language
from cadillac.runtime import _pick_strategy


def _mkfile(ws: str, rel: str, content: str = "") -> None:
    full = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(full) or ws, exist_ok=True)
    with open(full, "w") as f:
        f.write(textwrap.dedent(content))


class _Lang:
    """Lightweight stand-in so dispatch tests don't depend on the full Language dataclass."""
    def __init__(self, name: str, family: str, entry_point: str = "main.py"):
        self.name = name
        self.family = family
        self.entry_point = entry_point


class TestDispatchHTTP(unittest.TestCase):
    def test_flask_app_dispatches_http(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "main.py", '''\
                from flask import Flask
                app = Flask(__name__)
                @app.get("/")
                def root(): return "ok"
                if __name__ == "__main__":
                    app.run(port=5000)
            ''')
            strategy, reason = _pick_strategy(ws, python_language())
            self.assertEqual(strategy, "http")
            self.assertIn("backend", reason)


class TestDispatchCLI(unittest.TestCase):
    def test_python_cli_with_argparse(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "main.py", '''\
                import argparse
                def main():
                    p = argparse.ArgumentParser()
                    p.add_argument("name")
                    args = p.parse_args()
                    print(f"hello {args.name}")
                if __name__ == "__main__":
                    main()
            ''')
            strategy, reason = _pick_strategy(ws, python_language())
            self.assertEqual(strategy, "cli")

    def test_rust_bin_dispatches_cli(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "Cargo.toml", '[package]\nname="x"\nversion="0.1"\n')
            _mkfile(ws, "src/main.rs", "fn main() { println!(\"hi\"); }")
            strategy, _ = _pick_strategy(ws, _Lang("rust", "compiled"))
            self.assertEqual(strategy, "cli")


class TestDispatchLibrary(unittest.TestCase):
    def test_python_package_without_runner_is_library(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "mylib/__init__.py",
                    "from .core import foo\n__all__ = ['foo']\n")
            _mkfile(ws, "mylib/core.py", "def foo(): return 42\n")
            strategy, reason = _pick_strategy(ws, python_language())
            self.assertEqual(strategy, "library")

    def test_python_package_with_main_is_not_library(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "mylib/__init__.py", "from .core import foo\n")
            _mkfile(ws, "mylib/core.py", "def foo(): return 42\n")
            _mkfile(ws, "main.py",
                    "import argparse\n"
                    "if __name__ == '__main__':\n"
                    "    p = argparse.ArgumentParser(); p.parse_args()\n")
            strategy, _ = _pick_strategy(ws, python_language())
            self.assertEqual(strategy, "cli")  # main.py wins

    def test_rust_lib_only_dispatches_library(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "Cargo.toml",
                    '[package]\nname="x"\nversion="0.1"\n[lib]\nname="x"\n')
            _mkfile(ws, "src/lib.rs", "pub fn foo() -> i32 { 42 }")
            strategy, _ = _pick_strategy(ws, _Lang("rust", "compiled"))
            self.assertEqual(strategy, "library")


class TestDispatchSkip(unittest.TestCase):
    def test_static_now_dispatches_playwright(self):
        """Previously static family was skipped as "no runtime surface". With
        the playwright runner in place, a static site with an index.html is
        exactly the surface playwright can drive."""
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html", "<html></html>")
            strategy, reason = _pick_strategy(ws, _Lang("html", "static"))
            self.assertEqual(strategy, "playwright")
            self.assertIn("browser", reason.lower())

    def test_wordpress_skips(self):
        with tempfile.TemporaryDirectory() as ws:
            strategy, reason = _pick_strategy(ws, _Lang("wordpress", "php"))
            self.assertEqual(strategy, "skip")

    def test_browser_extension_skips(self):
        with tempfile.TemporaryDirectory() as ws:
            strategy, reason = _pick_strategy(ws, _Lang("browser_extension", "node"))
            self.assertEqual(strategy, "skip")

    def test_curses_interactive_skips(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "main.py", "import curses\ncurses.wrapper(lambda s: None)\n")
            strategy, reason = _pick_strategy(ws, python_language())
            self.assertEqual(strategy, "skip")
            self.assertIn("interactive", reason)

    def test_no_lang_skips(self):
        with tempfile.TemporaryDirectory() as ws:
            strategy, reason = _pick_strategy(ws, None)
            self.assertEqual(strategy, "skip")


if __name__ == "__main__":
    unittest.main()
