"""Tests for the WIRING phase's concurrent probe (Phase 1.1 of weakness fixes).

The probe boots no servers itself — it's invoked by `_wiring_dynamic_checks`
inside an already-booted-server context. These tests exercise the probe
function directly against a tiny synthetic Flask app and verify it surfaces
5xx-under-contention bugs that single-shot probes can't catch.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_listening(port: int, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


_RACING_FLASK_APP = '''\
import sys, time, threading
from flask import Flask, jsonify

app = Flask(__name__)

# Deliberately racy lazy init: two threads can both see _x is None and
# clobber each other. Mirrors the real "sqlite3 conn lazy-init" bug class.
_x = None
_x_count = 0

@app.route("/api/lazy")
def lazy():
    global _x, _x_count
    if _x is None:
        time.sleep(0.01)  # widen the race window so it's reliably triggerable
        _x = {"value": "init"}
        _x_count += 1
        if _x_count > 1:
            raise RuntimeError("lazy-init race: two threads both initialized")
    return jsonify(_x)

@app.route("/api/safe")
def safe():
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(sys.argv[1]), debug=False, threaded=True)
'''

_CLEAN_FLASK_APP = '''\
import sys
from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/api/safe")
def safe():
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(sys.argv[1]), debug=False, threaded=True)
'''


class TestConcurrentProbe(unittest.TestCase):
    """Verify the concurrent probe finds load-only bugs and doesn't false-positive."""

    @unittest.skipUnless(_have_flask := __import__("importlib").util.find_spec("flask"),
                          "Flask not installed in this env")
    def test_clean_app_passes(self):
        from cadillac.validate import _wiring_concurrent_probe
        from cadillac.contracts import Contract, Endpoint

        with tempfile.TemporaryDirectory() as td:
            Contract(endpoints=[
                Endpoint(name="safe", method="GET", path="/api/safe",
                         module="api", consumed_by=[]),
            ]).save(td)

            port = _free_port()
            app_file = os.path.join(td, "app.py")
            with open(app_file, "w") as f:
                f.write(_CLEAN_FLASK_APP)

            proc = subprocess.Popen(
                [sys.executable, app_file, str(port)],
                cwd=td, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                self.assertTrue(_wait_listening(port),
                                "test fixture flask app did not start")
                results: list = []
                summary = _wiring_concurrent_probe(port, "http://lan.test:5173",
                                                    td, results)
                errors = [r for r in results if not r.passed and r.severity == "error"]
                self.assertEqual(errors, [],
                                 f"clean app should not 5xx under load: "
                                 f"{[r.output for r in errors]}")
                self.assertIn("no 5xx leaks", summary)
            finally:
                import signal
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                proc.wait(timeout=2)

    @unittest.skipUnless(_have_flask := __import__("importlib").util.find_spec("flask"),
                          "Flask not installed in this env")
    def test_racy_app_caught(self):
        from cadillac.validate import _wiring_concurrent_probe
        from cadillac.contracts import Contract, Endpoint

        with tempfile.TemporaryDirectory() as td:
            Contract(endpoints=[
                Endpoint(name="lazy", method="GET", path="/api/lazy",
                         module="api", consumed_by=[]),
                Endpoint(name="safe", method="GET", path="/api/safe",
                         module="api", consumed_by=[]),
            ]).save(td)

            port = _free_port()
            app_file = os.path.join(td, "app.py")
            with open(app_file, "w") as f:
                f.write(_RACING_FLASK_APP)

            proc = subprocess.Popen(
                [sys.executable, app_file, str(port)],
                cwd=td, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                self.assertTrue(_wait_listening(port))
                results: list = []
                _wiring_concurrent_probe(port, "http://lan.test:5173", td, results)
                errors = [r for r in results if not r.passed and r.severity == "error"]
                self.assertTrue(
                    any("5xx under concurrent load" in r.output for r in errors),
                    f"expected lazy-init race to be flagged; got: "
                    f"{[r.output for r in results]}",
                )
            finally:
                import signal
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                proc.wait(timeout=2)

    def test_no_contract_returns_empty(self):
        """No contracts.json — probe should no-op (nothing to hammer)."""
        from cadillac.validate import _wiring_concurrent_probe
        with tempfile.TemporaryDirectory() as td:
            results: list = []
            summary = _wiring_concurrent_probe(99999, "http://lan.test:5173",
                                                td, results)
            self.assertEqual(summary, "")
            self.assertEqual(results, [])

    def test_only_param_endpoints_returns_empty(self):
        """Contract has only `<param>`-style GETs — no safe targets to hit."""
        from cadillac.validate import _wiring_concurrent_probe
        from cadillac.contracts import Contract, Endpoint
        with tempfile.TemporaryDirectory() as td:
            Contract(endpoints=[
                Endpoint(name="show", method="GET", path="/api/items/<id>",
                         module="api", consumed_by=[]),
            ]).save(td)
            results: list = []
            summary = _wiring_concurrent_probe(99999, "http://lan.test:5173",
                                                td, results)
            self.assertEqual(summary, "")
            self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
