"""Round 3 of the audit: modular plan consistency (H5, H6, H8)."""

import os
import unittest

from cadillac.modules import ModularPlan, ModuleSpec


def _plan(modules: list[dict], **kw) -> dict:
    return {
        "modular": True,
        "modules": modules,
        "module_build_order": [[m["name"] for m in modules]],
        "integration_files": [],
        "entry_point": kw.get("entry_point", "main.py"),
        "test_file": kw.get("test_file", "test_main.py"),
        "constraints": [],
    }


class TestPhantomDependencyWarning(unittest.TestCase):
    """H8: missing depends_on must surface loudly, not be silently skipped."""

    def test_phantom_dep_shows_warning_in_interface_stubs(self):
        plan = _plan([
            {"name": "auth", "path": "auth/", "purpose": "x",
             "depends_on": [], "exports": [], "interfaces": [],
             "files": [], "build_order": []},
            {"name": "api", "path": "api/", "purpose": "x",
             "depends_on": ["ghost", "auth"],
             "exports": [], "interfaces": [],
             "files": [], "build_order": []},
        ])
        mp = ModularPlan.from_dict(plan)
        stubs = mp.get_dependency_interfaces("api")
        self.assertIn("ghost", stubs)
        self.assertIn("WARNING", stubs)
        self.assertIn("no such module exists", stubs)

    def test_phantom_dep_recorded_on_module(self):
        plan = _plan([
            {"name": "api", "path": "api/", "purpose": "x",
             "depends_on": ["ghost"],
             "exports": [], "interfaces": [],
             "files": [], "build_order": []},
        ])
        mp = ModularPlan.from_dict(plan)
        mp.get_dependency_interfaces("api")  # populates missing_deps
        api = mp.get_module("api")
        self.assertEqual(getattr(api, "missing_deps", None), ["ghost"])

    def test_real_dep_not_flagged(self):
        plan = _plan([
            {"name": "auth", "path": "auth/", "purpose": "x",
             "depends_on": [], "exports": [], "interfaces": [],
             "files": [], "build_order": []},
            {"name": "api", "path": "api/", "purpose": "x",
             "depends_on": ["auth"],
             "exports": [], "interfaces": [],
             "files": [], "build_order": []},
        ])
        mp = ModularPlan.from_dict(plan)
        stubs = mp.get_dependency_interfaces("api")
        self.assertNotIn("WARNING", stubs)


class TestEngineFlatPlanClearsModularPlan(unittest.TestCase):
    """H5: engine source must clear modular_plan when accepting a flat plan.

    We pin this at the source level rather than running a full build:
    locating the flat-plan-success block and verifying it sets
    modular_plan = None.
    """

    def test_engine_resets_modular_plan_on_flat_acceptance(self):
        with open("/home/waive3/sandbox/cadillac/engine.py") as f:
            src = f.read()
        # Find the flat-plan handling block — it has the "[Manifest:" log
        # and the comment "# Standard flat plan"
        idx = src.find("# Standard flat plan")
        self.assertGreater(idx, 0, "could not locate flat-plan block")
        # Within ~3000 chars after that, modular_plan = None must appear
        block = src[idx:idx + 3000]
        self.assertIn("modular_plan = None", block,
                      "flat-plan acceptance must clear modular_plan (H5)")


class TestEngineHardStopsOnRepeatedManifestFailure(unittest.TestCase):
    """H6: when both modular and flat manifests have failed multiple times,
    the PLAN loop must surface a clean abort instead of looping forever."""

    def test_engine_has_hard_stop_termination(self):
        with open("/home/waive3/sandbox/cadillac/engine.py") as f:
            src = f.read()
        # Counter must exist
        self.assertIn("flat_manifest_failures", src,
                      "engine must track flat-plan failures (H6)")
        # Termination message
        self.assertIn(
            "both modular and flat manifest", src,
            "engine must emit a clean termination when both shapes fail (H6)",
        )
        # progress.phase = STOPPED
        self.assertIn('progress.phase = "STOPPED"', src,
                      "termination must mark progress as STOPPED")


if __name__ == "__main__":
    unittest.main()
