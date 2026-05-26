"""Round 8: three findings from the latest live limit-tests.

Pin each fix at the source level — these are prompt + scaffolding changes
that wouldn't show up in a lighter behavioral test.
"""

import unittest

from cadillac import engine as _engine_mod

_ENGINE_PATH = _engine_mod.__file__


class TestRustTestLayoutGuidance(unittest.TestCase):
    """Limit-test 1 (TomlDiff) finding: LLM scaffolded nested
    src/<module>/src/lib.rs which Rust accepts but cargo test from the
    workspace root doesn't pick up. Prompt should warn against it."""

    def test_project_structure_warns_about_nested_crates(self):
        from cadillac.quality import RUST_PROJECT_STRUCTURE
        self.assertIn("DO NOT create a nested cargo crate", RUST_PROJECT_STRUCTURE)
        self.assertIn("running 0 tests", RUST_PROJECT_STRUCTURE)

    def test_project_structure_states_where_unit_tests_live(self):
        from cadillac.quality import RUST_PROJECT_STRUCTURE
        self.assertIn("WHERE TESTS GO", RUST_PROJECT_STRUCTURE)
        self.assertIn("#[cfg(test)] mod tests", RUST_PROJECT_STRUCTURE)

    def test_anti_patterns_calls_out_nested_crate(self):
        from cadillac.quality import RUST_ANTI_PATTERNS
        self.assertIn("nested `src/<module>/src/lib.rs`", RUST_ANTI_PATTERNS)


class TestBrowserExtensionPluginPin(unittest.TestCase):
    """Limit-test 2 (TabNotes) finding: vite-plugin-web-extension's CJS
    loading bug breaks vite build. Prompt + engine scaffold should pin
    a known-working plugin version."""

    def test_coding_standards_recommends_pinned_version(self):
        from cadillac.quality import BROWSER_EXT_CODING_STANDARDS
        # Should mention either @crxjs version pin or vite-plugin-web-extension version pin
        self.assertTrue(
            "@crxjs/vite-plugin@^2" in BROWSER_EXT_CODING_STANDARDS
            or "vite-plugin-web-extension@^4.4" in BROWSER_EXT_CODING_STANDARDS,
            "browser-extension prompt must pin a working plugin version",
        )

    def test_engine_scaffold_pins_crxjs(self):
        """When language is browser_extension, engine.py's package.json
        scaffolding must include @crxjs/vite-plugin pinned to ^2."""
        with open(_ENGINE_PATH) as f:
            src = f.read()
        idx = src.find('lang.name == "browser_extension"')
        self.assertGreater(idx, 0,
                           "engine.py must have a dedicated browser_extension branch")
        block = src[idx:idx + 1500]
        self.assertIn('"@crxjs/vite-plugin": "^2"', block,
                      "browser-extension scaffold must pin @crxjs/vite-plugin to ^2")
        # And specific Vite/vitest versions, not "*"
        self.assertIn('"vite": "^5"', block)
        self.assertIn('"vitest": "^1"', block)


class TestMultiLanguageNpmInstall(unittest.TestCase):
    """Limit-test 3 (ImageClassifier) follow-up: with the
    add_dep-bootstrap fix, frontend/package.json gets created in a
    Python-primary workspace, but `npm install --prefix frontend` was
    never run, so frontend tsc/vitest couldn't resolve react/vite.
    Engine should detect this case and run the install."""

    def test_engine_runs_npm_install_in_frontend_subdir(self):
        with open(_ENGINE_PATH) as f:
            src = f.read()
        # Locate the multi-language sweep block
        idx = src.find("Multi-language sweep")
        self.assertGreater(idx, 0,
                           "engine.py must have a multi-language sweep that "
                           "runs npm install in subdir(s) when frontend/"
                           "package.json exists in a non-node-family workspace")
        block = src[idx:idx + 1500]
        # Must check the well-known subdir names
        for subdir in ("frontend", "client", "web", "ui"):
            self.assertIn(f'"{subdir}"', block,
                          f"sweep should consider {subdir}/ as a frontend dir")
        # Must check for the absence of node_modules (else we'd reinstall every build)
        self.assertIn("node_modules", block)
        # Must run `cd frontend && npm install` (or equivalent)
        self.assertIn("npm install", block)

    def test_engine_skips_when_node_modules_already_present(self):
        """The sweep should be a no-op when node_modules already exists,
        so we don't reinstall on every iterate/build cycle."""
        with open(_ENGINE_PATH) as f:
            src = f.read()
        idx = src.find("Multi-language sweep")
        block = src[idx:idx + 1500]
        # The condition checks BOTH package.json exists AND node_modules doesn't
        self.assertIn("isfile(sub_pkg)", block)
        self.assertIn("not os.path.isdir(sub_nm)", block)


if __name__ == "__main__":
    unittest.main()
