"""Tests for dependency-install isolation.

Build 12 generated a requirements.txt pinning `pydantic==2.6.1` /
`pydantic-settings==2.1.0` and installed it with the HOST interpreter. That
downgraded the machine's own packages from 2.12.5 / 2.13.1 and broke the
unrelated `mcp` SDK for everything on the box — 4 previously-passing tests in
this very suite started failing, and they fail identically at the pre-session
commit, which is how it was traced to the environment rather than the code.

An unattended builder must not be able to mutate the host. Installs now go to a
workspace-local venv; deleting the workspace fully undoes them.
"""

import os
import subprocess
import sys
import tempfile
import unittest

from cadillac.manifest import FileManifest
from cadillac.tools import (
    ToolExecutor,
    _redirect_pip_to_venv,
    _venv_env,
    validate_command,
    workspace_venv_python,
)


class TestPipRedirect(unittest.TestCase):
    """Only the DESTINATION changes; the build's intent is preserved."""

    VP = "/ws/.venv/bin/python3"

    def test_pip3_install(self):
        self.assertEqual(
            _redirect_pip_to_venv("pip3 install fastapi", self.VP),
            f"{self.VP} -m pip install fastapi")

    def test_bare_pip_install(self):
        self.assertEqual(
            _redirect_pip_to_venv("pip install fastapi", self.VP),
            f"{self.VP} -m pip install fastapi")

    def test_python_m_pip_install(self):
        self.assertEqual(
            _redirect_pip_to_venv("python3 -m pip install pytest -q", self.VP),
            f"{self.VP} -m pip install pytest -q")

    def test_requirements_file_is_preserved(self):
        """The exact shape that caused the damage."""
        self.assertEqual(
            _redirect_pip_to_venv("pip3 install -r requirements.txt", self.VP),
            f"{self.VP} -m pip install -r requirements.txt")

    def test_flags_and_pins_survive(self):
        out = _redirect_pip_to_venv("pip3 install 'pydantic==2.6.1' --quiet", self.VP)
        self.assertIn("pydantic==2.6.1", out)
        self.assertIn("--quiet", out)

    def test_break_system_packages_is_stripped(self):
        """It exists only to defeat the host's PEP 668 guard."""
        out = _redirect_pip_to_venv(
            "pip3 install pytest --break-system-packages", self.VP)
        self.assertNotIn("--break-system-packages", out)

    def test_chained_command_is_rewritten_in_place(self):
        out = _redirect_pip_to_venv("cd sub && pip3 install -r req.txt", self.VP)
        self.assertTrue(out.startswith("cd sub && "))
        self.assertIn(f"{self.VP} -m pip install", out)

    def test_non_install_pip_is_untouched(self):
        for cmd in ("pip3 list", "pip3 --version", "pip3 show six"):
            self.assertEqual(_redirect_pip_to_venv(cmd, self.VP), cmd)

    def test_rewritten_command_passes_the_allowlist(self):
        """The rewrite must not create a command the policy then rejects —
        the allowlist has `python3`, not `python`, which is why the venv's
        python3 symlink is the one used."""
        out = _redirect_pip_to_venv("pip3 install -r requirements.txt", self.VP)
        allowed, why = validate_command(out, workspace="/ws")
        self.assertTrue(allowed, why)


class TestVenvEnv(unittest.TestCase):
    def test_venv_bin_goes_first_on_path(self):
        with tempfile.TemporaryDirectory() as ws:
            os.makedirs(os.path.join(ws, ".venv", "bin"))
            env = _venv_env(ws)
            self.assertTrue(env["PATH"].startswith(os.path.join(ws, ".venv", "bin")))
            self.assertEqual(env["VIRTUAL_ENV"], os.path.join(ws, ".venv"))

    def test_no_venv_leaves_path_alone(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(_venv_env(ws)["PATH"], os.environ.get("PATH", ""))

    def test_stale_pythonhome_is_dropped(self):
        with tempfile.TemporaryDirectory() as ws:
            os.makedirs(os.path.join(ws, ".venv", "bin"))
            os.environ["PYTHONHOME"] = "/bogus"
            try:
                self.assertNotIn("PYTHONHOME", _venv_env(ws))
            finally:
                os.environ.pop("PYTHONHOME", None)


class TestVenvCreationVerifiesPip(unittest.TestCase):
    """`python3 -m venv` on a host without python3-venv creates the interpreter
    symlinks and then fails at ensurepip, leaving a venv with NO pip. Trusting
    the return code accepted that half-built state."""

    def test_creates_a_venv_with_pip(self):
        with tempfile.TemporaryDirectory() as ws:
            py = workspace_venv_python(ws)
            self.assertIsNotNone(py, "no usable venv creator on this host")
            self.assertTrue(os.path.exists(py))
            self.assertTrue(os.path.exists(os.path.join(ws, ".venv", "bin", "pip")),
                            "a venv without pip is unusable and must not be accepted")

    def test_second_call_reuses_the_venv(self):
        with tempfile.TemporaryDirectory() as ws:
            first = workspace_venv_python(ws)
            marker = os.path.join(ws, ".venv", "reuse-marker")
            open(marker, "w").close()
            self.assertEqual(workspace_venv_python(ws), first)
            self.assertTrue(os.path.exists(marker), "the venv was rebuilt")

    def test_half_built_venv_is_rejected(self):
        """Interpreter present, pip absent — must not be handed back."""
        with tempfile.TemporaryDirectory() as ws:
            bindir = os.path.join(ws, ".venv", "bin")
            os.makedirs(bindir)
            open(os.path.join(bindir, "python3"), "w").close()
            # No pip -> must rebuild or refuse, never return this as-is.
            result = workspace_venv_python(ws)
            if result is not None:
                self.assertTrue(os.path.exists(os.path.join(bindir, "pip")))


class TestHostIsNeverMutated(unittest.TestCase):
    """The property that actually matters."""

    def _host_version(self, pkg):
        r = subprocess.run(
            [sys.executable, "-c",
             f"import importlib.metadata as m; print(m.version('{pkg}'))"],
            capture_output=True, text=True)
        return r.stdout.strip() or None

    def test_a_downgrade_does_not_touch_the_host(self):
        """Reproduces the exact damage: installing an OLDER pin."""
        before = self._host_version("six")
        if not before or before == "1.12.0":
            self.skipTest("host 'six' unavailable or already at the test version")
        with tempfile.TemporaryDirectory() as ws:
            ex = ToolExecutor(ws, FileManifest())
            ex.config_writes_allowed = True
            result = ex.run_command("pip3 install six==1.12.0 -q")
            self.assertEqual(result.get("exit_code"), 0,
                             (result.get("stderr") or "")[:200])
            venv_py = os.path.join(ws, ".venv", "bin", "python3")
            got = subprocess.run(
                [venv_py, "-c", "import six; print(six.__version__)"],
                capture_output=True, text=True).stdout.strip()
        self.assertEqual(self._host_version("six"), before,
                         "the host package was modified")
        self.assertEqual(got, "1.12.0", "the install did not land in the venv")

    def test_install_is_refused_when_no_venv_can_be_made(self):
        """Fail CLOSED — falling back to the host is the damage being prevented."""
        import cadillac.tools as tools

        original = tools.workspace_venv_python
        tools.workspace_venv_python = lambda ws: None
        try:
            with tempfile.TemporaryDirectory() as ws:
                ex = ToolExecutor(ws, FileManifest())
                result = ex.run_command("pip3 install six")
                self.assertEqual(result.get("exit_code"), 1)
                self.assertIn("Refusing to install", result.get("stderr", ""))
        finally:
            tools.workspace_venv_python = original

    def test_non_install_commands_still_run_normally(self):
        with tempfile.TemporaryDirectory() as ws:
            ex = ToolExecutor(ws, FileManifest())
            self.assertEqual(ex.run_command("echo hello").get("exit_code"), 0)


if __name__ == "__main__":
    unittest.main()
