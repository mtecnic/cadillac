"""Tests for the operational gates (cadillac/operational.py).

Three gates under test:
  - Gate 1: static schema integrity (AST scan)
  - Gate 2: missing-env runtime probe (boots a synthetic Flask app)
  - Gate 3: SIGTERM responsiveness (boots a synthetic Flask app)

Gates 2 & 3 boot real subprocesses on dynamic ports; they take 2-15 seconds
each. Static tests run instantly. Both runtime tests skip if Flask is not
importable in the harness's interpreter.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from cadillac.languages import python_language
from cadillac.operational import (
    _find_none_passed_to_not_null,
    _parse_create_table,
    _runtime_probe_missing_env,
    _runtime_probe_sigterm,
    _scan_required_env_vars,
    _scan_schemas,
    _static_schema_integrity_check,
    check_operational_gates,
)


def _flask_available() -> bool:
    try:
        import flask  # noqa: F401
        return True
    except ImportError:
        return False


class TestParseCreateTable(unittest.TestCase):
    def test_basic_not_null(self):
        cols = _parse_create_table(
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, "
            "email TEXT NOT NULL UNIQUE, "
            "bio TEXT"
        )
        by_name = {c.name: c for c in cols}
        self.assertTrue(by_name["name"].not_null)
        self.assertTrue(by_name["email"].not_null)
        self.assertFalse(by_name["bio"].not_null)
        # PK is implicitly not-required
        self.assertTrue(by_name["id"].has_default)

    def test_skips_table_constraints(self):
        cols = _parse_create_table(
            "id INTEGER, "
            "ref_id INTEGER NOT NULL, "
            "FOREIGN KEY (ref_id) REFERENCES other(id), "
            "UNIQUE (id, ref_id)"
        )
        names = [c.name for c in cols]
        self.assertEqual(names, ["id", "ref_id"])

    def test_handles_defaults(self):
        cols = _parse_create_table(
            "name TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'pending'"
        )
        by_name = {c.name: c for c in cols}
        self.assertFalse(by_name["name"].has_default)
        self.assertTrue(by_name["status"].has_default)


class TestSchemaIntegrityStatic(unittest.TestCase):
    def _writefile(self, ws: str, rel: str, content: str) -> None:
        full = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(textwrap.dedent(content))

    def test_pingflux_bug_class_caught(self):
        """The literal bug class: status_code INTEGER NOT NULL receives None."""
        with tempfile.TemporaryDirectory() as ws:
            self._writefile(ws, "writer.py", '''\
                import sqlite3

                def setup(conn):
                    conn.execute("""CREATE TABLE ping_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        target TEXT NOT NULL,
                        status_code INTEGER NOT NULL,
                        latency_ms REAL
                    )""")

                def record_failure(conn, target):
                    conn.execute(
                        "INSERT INTO ping_results (target, status_code, latency_ms) VALUES (?, ?, ?)",
                        (target, None, None),
                    )
            ''')
            results = _static_schema_integrity_check(ws, python_language())
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0].passed)
            self.assertIn("status_code", results[0].output)
            self.assertIn("ping_results", results[0].output)
            # latency_ms is nullable — must NOT be flagged
            self.assertNotIn("latency_ms", results[0].output)

    def test_clean_code_passes(self):
        with tempfile.TemporaryDirectory() as ws:
            self._writefile(ws, "writer.py", '''\
                def setup(conn):
                    conn.execute("CREATE TABLE x (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")

                def add(conn, name):
                    conn.execute("INSERT INTO x (name) VALUES (?)", (name,))
            ''')
            results = _static_schema_integrity_check(ws, python_language())
            self.assertTrue(results[0].passed)

    def test_no_schemas_skips(self):
        with tempfile.TemporaryDirectory() as ws:
            self._writefile(ws, "x.py", "print('hello')\n")
            results = _static_schema_integrity_check(ws, python_language())
            self.assertTrue(results[0].passed)
            self.assertIn("no CREATE TABLE", results[0].output)

    def test_default_column_not_flagged(self):
        """Column with DEFAULT can receive None — SQLite fills the default."""
        with tempfile.TemporaryDirectory() as ws:
            self._writefile(ws, "x.py", '''\
                def setup(c):
                    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'ok')")
                def go(c):
                    c.execute("INSERT INTO t (status) VALUES (?)", (None,))
            ''')
            results = _static_schema_integrity_check(ws, python_language())
            self.assertTrue(results[0].passed)


class TestScanRequiredEnv(unittest.TestCase):
    def test_finds_strict_environ_access(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "config.py"), "w") as f:
                f.write(textwrap.dedent('''\
                    import os
                    DB_URL = os.environ["DATABASE_URL"]
                    SECRET = os.environ.get("APP_SECRET", "dev")
                    PORT = os.environ.get("PORT")
                    OPTIONAL = os.environ["OPTIONAL_FEATURE"]
                '''))
            required = _scan_required_env_vars(ws)
            self.assertIn("DATABASE_URL", required)
            self.assertIn("OPTIONAL_FEATURE", required)
            self.assertNotIn("APP_SECRET", required)
            self.assertNotIn("PORT", required)

    def test_ignores_system_vars(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "x.py"), "w") as f:
                f.write('import os\nx = os.environ["PATH"]\n')
            required = _scan_required_env_vars(ws)
            self.assertNotIn("PATH", required)


# ── Runtime probes ────────────────────────────────────────────────────────────


_FLASK_NEEDS_ENV = '''\
import os
from flask import Flask, jsonify

# Strict required at import — boot crashes with KeyError if not set
DATABASE_URL = os.environ["DATABASE_URL"]

app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify(ok=True, db=DATABASE_URL)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
'''


_FLASK_GRACEFUL = '''\
import os
from flask import Flask
app = Flask(__name__)
@app.get("/")
def root(): return "ok"
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
'''


_FLASK_IGNORES_SIGTERM = '''\
import os
import signal
import time
from flask import Flask

# Bad: swallow SIGTERM and don't exit.
def _swallow(signum, frame):
    pass
signal.signal(signal.SIGTERM, _swallow)

app = Flask(__name__)
@app.get("/")
def root(): return "ok"
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
'''


@unittest.skipUnless(_flask_available(), "Flask not installed in test interpreter")
class TestMissingEnvProbe(unittest.TestCase):
    def test_clean_failfast_passes(self):
        """Backend that raises KeyError naming the var → pass."""
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "main.py"), "w") as f:
                f.write(_FLASK_NEEDS_ENV)
            os.environ.pop("DATABASE_URL", None)  # ensure not in test env
            be = {"cmd": [sys.executable, "main.py"], "port": 5000}
            results = _runtime_probe_missing_env(ws, python_language(), be)
            self.assertEqual(len(results), 1)
            self.assertTrue(
                results[0].passed,
                f"clean fail-fast should pass; got: {results[0].output}",
            )

    def test_no_strict_env_skips(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "main.py"), "w") as f:
                f.write(_FLASK_GRACEFUL)
            be = {"cmd": [sys.executable, "main.py"], "port": 5000}
            results = _runtime_probe_missing_env(ws, python_language(), be)
            self.assertTrue(results[0].passed)
            self.assertIn("no strict os.environ", results[0].output)


@unittest.skipUnless(_flask_available(), "Flask not installed in test interpreter")
class TestSigtermProbe(unittest.TestCase):
    def test_responsive_backend_passes(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "main.py"), "w") as f:
                f.write(_FLASK_GRACEFUL)
            be = {"cmd": [sys.executable, "main.py"], "port": 5000}
            results = _runtime_probe_sigterm(ws, python_language(), be)
            self.assertEqual(len(results), 1)
            self.assertTrue(
                results[0].passed,
                f"graceful backend should pass; got: {results[0].output}",
            )

    def test_swallowing_sigterm_fails(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "main.py"), "w") as f:
                f.write(_FLASK_IGNORES_SIGTERM)
            be = {"cmd": [sys.executable, "main.py"], "port": 5000}
            results = _runtime_probe_sigterm(ws, python_language(), be)
            self.assertEqual(len(results), 1)
            self.assertFalse(
                results[0].passed,
                "SIGTERM-swallowing backend should fail this gate",
            )
            self.assertIn("ignored SIGTERM", results[0].output)


class TestOrchestratorSkips(unittest.TestCase):
    def test_non_python_lang_runtime_gates_skipped(self):
        """Non-Python langs skip runtime probes but still get a result entry."""
        # Build a stub lang object
        class L:
            name = "rust"
            family = "compiled"
        with tempfile.TemporaryDirectory() as ws:
            results = check_operational_gates(ws, L())
            # Should produce at least the static-skip + runtime-skip messages
            self.assertTrue(all(r.passed for r in results))
            self.assertTrue(any("skipped" in r.output for r in results))

    def test_python_cli_no_backend_skips_runtime(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "tool.py"), "w") as f:
                f.write("def main():\n    print('hi')\n\nif __name__ == '__main__':\n    main()\n")
            results = check_operational_gates(ws, python_language())
            # All should pass (no schema, no backend)
            self.assertTrue(all(r.passed for r in results))
            self.assertTrue(any("no HTTP backend" in r.output for r in results))


if __name__ == "__main__":
    unittest.main()
