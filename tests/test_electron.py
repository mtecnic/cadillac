"""Tests for Electron language support (Windows-native desktop apps)."""

import unittest

from cadillac.languages import (
    detect_language,
    electron_language,
    react_language,
)


class TestElectronLanguage(unittest.TestCase):
    def test_shape(self):
        lang = electron_language()
        self.assertEqual(lang.name, "electron")
        # Reuses the same node toolchain (npm, tsc, vitest, eslint).
        # Family-keyed dispatches in validate.py and engine.py fire as for react/vue/angular.
        self.assertEqual(lang.family, "node")
        self.assertEqual(lang.entry_point, "src/main/main.ts")
        self.assertEqual(lang.run_cmd, "npx electron .")
        self.assertEqual(lang.test_cmd, ["npx", "vitest", "run"])
        # build_cmd intentionally only runs the renderer build — electron-builder
        # is too heavy for validation; users/CI run it separately.
        self.assertEqual(lang.build_cmd, "npx vite build")
        self.assertEqual(lang.package_file, "package.json")

    def test_quality_content_wired(self):
        lang = electron_language()
        # Each of these is a free-form string from quality.py — just confirm
        # the wiring is in place (non-empty, no stray REACT_ leakage).
        self.assertTrue(lang.coding_standards)
        self.assertTrue(lang.project_structure)
        self.assertTrue(lang.anti_patterns)
        self.assertIn("preload", lang.coding_standards.lower())
        self.assertIn("contextisolation", lang.coding_standards.lower())
        self.assertIn("src/main", lang.project_structure)
        self.assertIn("src/renderer", lang.project_structure)
        self.assertIn("nodeintegration", lang.anti_patterns.lower())


class TestElectronDetection(unittest.TestCase):
    def test_electron_keyword_routes_to_electron(self):
        self.assertEqual(detect_language("Build with Electron + React").name, "electron")
        self.assertEqual(detect_language("electron desktop app").name, "electron")

    def test_windows_app_keywords_route_to_electron(self):
        self.assertEqual(detect_language("Build a Windows app").name, "electron")
        self.assertEqual(detect_language("native desktop app").name, "electron")
        self.assertEqual(detect_language("cross-platform desktop tool").name, "electron")
        self.assertEqual(detect_language("Windows desktop tool").name, "electron")

    def test_electron_takes_priority_over_react(self):
        """A task mentioning BOTH electron and react must route to electron.

        Otherwise the wrong toolchain is selected: react_language() wires the
        renderer for vite-only, and the autobuilt scaffold has no main process
        / preload / electron-builder config — the project doesn't run as a
        desktop app at all.
        """
        lang = detect_language(
            "Build a Windows desktop app using Electron + React + TypeScript"
        )
        self.assertEqual(lang.name, "electron")

    def test_plain_react_still_routes_to_react(self):
        # Sanity: electron's added keywords must not eat plain-react tasks.
        self.assertEqual(detect_language("Build a React SPA").name, "react")
        self.assertEqual(
            detect_language("a React + Vite todo app with vitest").name, "react"
        )

    def test_react_and_electron_are_distinct(self):
        # Trivial but worth pinning — if someone clones electron_language()
        # from react_language() and forgets to change the name, this fails.
        self.assertNotEqual(electron_language().name, react_language().name)
        self.assertNotEqual(
            electron_language().entry_point, react_language().entry_point
        )


if __name__ == "__main__":
    unittest.main()
