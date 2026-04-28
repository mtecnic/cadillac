"""Unit tests for topology.py (Phase 3: orchestrator-owned cross-module graph)."""

import json
import os
import tempfile
import unittest

from cadillac.contracts import Contract, Endpoint
from cadillac.topology import Topology, TopologyIssue


def _write(td: str, rel: str, content: str) -> None:
    path = os.path.join(td, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


class TestEnvVarLifecycle(unittest.TestCase):
    def test_unloaded_env_var_with_no_default_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "backend/app.py", (
                "import os\n"
                "secret = os.environ.get('MY_API_KEY')\n"
                # ^ no default, no load site, not ambient → flagged
            ))
            topo = Topology.build(td)
            issues = topo.check()
            self.assertTrue(any(i.rule == "env_var_unloaded" and "MY_API_KEY" in i.message
                                for i in issues),
                            f"expected env_var_unloaded, got: {[i.message for i in issues]}")

    def test_env_var_with_default_is_fine(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "backend/app.py", (
                "import os\n"
                "host = os.environ.get('MY_HOST', '0.0.0.0')\n"
            ))
            topo = Topology.build(td)
            self.assertEqual([i for i in topo.check() if i.rule == "env_var_unloaded"], [])

    def test_env_var_with_load_site_is_fine(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "backend/app.py", (
                "import os\n"
                "os.environ['MY_API_KEY'] = 'sentinel'\n"
                "secret = os.environ.get('MY_API_KEY')\n"
            ))
            topo = Topology.build(td)
            self.assertEqual([i for i in topo.check() if i.rule == "env_var_unloaded"], [])

    def test_dotenv_defers_check(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "backend/app.py", (
                "from dotenv import load_dotenv\n"
                "import os\n"
                "load_dotenv()\n"
                "secret = os.environ.get('MY_API_KEY')\n"
            ))
            topo = Topology.build(td)
            self.assertEqual([i for i in topo.check() if i.rule == "env_var_unloaded"], [])

    def test_ambient_vars_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "backend/app.py", (
                "import os\n"
                "p = os.environ.get('PORT')\n"
                "h = os.environ.get('HOST')\n"
                "fe = os.environ.get('FLASK_ENV')\n"
            ))
            topo = Topology.build(td)
            self.assertEqual([i for i in topo.check() if i.rule == "env_var_unloaded"], [])


class TestConfigKeyLifecycle(unittest.TestCase):
    """Catches the exact CORS_ORIGINS bug we hit on the eBay clone."""

    def test_dead_lookup_with_no_default_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "backend/app.py", (
                "from flask import Flask\n"
                "app = Flask(__name__)\n"
                "origins = app.config.get('CORS_ORIGINS')\n"
                # ^ no default, no write site → dead lookup
            ))
            topo = Topology.build(td)
            issues = topo.check()
            self.assertTrue(any(i.rule == "config_key_dead_lookup" and "CORS_ORIGINS" in i.message
                                for i in issues),
                            f"expected dead_lookup for CORS_ORIGINS, got: {[i.message for i in issues]}")

    def test_lookup_with_default_is_fine(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "backend/app.py", (
                "from flask import Flask\n"
                "app = Flask(__name__)\n"
                "origins = app.config.get('CORS_ORIGINS', '*')\n"
            ))
            topo = Topology.build(td)
            self.assertEqual([i for i in topo.check() if i.rule == "config_key_dead_lookup"], [])

    def test_lookup_with_explicit_write_is_fine(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "backend/app.py", (
                "from flask import Flask\n"
                "app = Flask(__name__)\n"
                "app.config['CORS_ORIGINS'] = 'http://localhost:5173'\n"
                "origins = app.config.get('CORS_ORIGINS')\n"
            ))
            topo = Topology.build(td)
            self.assertEqual([i for i in topo.check() if i.rule == "config_key_dead_lookup"], [])

    def test_from_object_defers_check(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "backend/app.py", (
                "from flask import Flask\n"
                "import config\n"
                "app = Flask(__name__)\n"
                "app.config.from_object(config)\n"
                "origins = app.config.get('CORS_ORIGINS')\n"
            ))
            topo = Topology.build(td)
            self.assertEqual([i for i in topo.check() if i.rule == "config_key_dead_lookup"], [])


class TestContractModuleRefs(unittest.TestCase):
    def test_phantom_module_owner_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            # Plan with modules: only "auth" exists
            plan = {
                "modular": True,
                "modules": [
                    {"name": "auth", "path": "auth/", "purpose": "x",
                     "depends_on": [], "exports": [], "interfaces": [],
                     "files": [], "build_order": []},
                ],
                "module_build_order": [["auth"]],
                "integration_files": [],
                "entry_point": "main.py",
                "test_file": "test_main.py",
            }
            with open(os.path.join(td, "plan.json"), "w") as f:
                json.dump(plan, f)
            # Contract references "ghost" module that doesn't exist
            Contract(endpoints=[
                Endpoint(name="x", method="POST", path="/api/x",
                         module="ghost", consumed_by=["auth"]),
            ]).save(td)
            topo = Topology.build(td)
            issues = topo.check()
            self.assertTrue(any(i.rule == "phantom_module_owner" for i in issues),
                            f"expected phantom_module_owner, got: {[i.rule for i in issues]}")

    def test_phantom_consumer_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            plan = {
                "modular": True,
                "modules": [
                    {"name": "auth", "path": "auth/", "purpose": "x",
                     "depends_on": [], "exports": [], "interfaces": [],
                     "files": [], "build_order": []},
                ],
                "module_build_order": [["auth"]],
                "integration_files": [],
                "entry_point": "main.py",
                "test_file": "test_main.py",
            }
            with open(os.path.join(td, "plan.json"), "w") as f:
                json.dump(plan, f)
            Contract(endpoints=[
                Endpoint(name="x", method="POST", path="/api/x",
                         module="auth", consumed_by=["nonexistent"]),
            ]).save(td)
            topo = Topology.build(td)
            issues = topo.check()
            self.assertTrue(any(i.rule == "phantom_module_consumer" for i in issues))


if __name__ == "__main__":
    unittest.main()
