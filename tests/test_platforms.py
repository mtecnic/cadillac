"""Tests for WordPress plugin + Chrome MV3 browser-extension support
(Phase 2 of the weakness roadmap)."""

import unittest

from cadillac.languages import (
    browser_extension_language,
    detect_language,
    react_language,
    wordpress_language,
)


class TestWordpressLanguage(unittest.TestCase):
    def test_shape(self):
        lang = wordpress_language()
        self.assertEqual(lang.name, "wordpress")
        self.assertEqual(lang.family, "php")
        self.assertEqual(lang.extensions, [".php"])
        self.assertEqual(lang.syntax_check_cmd, ["php", "-l"])
        # Quality content present and on-topic
        self.assertIn("Plugin Name", lang.coding_standards)
        self.assertIn("$wpdb->prepare", lang.anti_patterns)
        self.assertIn("manage_options", lang.few_shot_main)

    def test_detection(self):
        self.assertEqual(detect_language("Build a WordPress plugin").name, "wordpress")
        self.assertEqual(detect_language("a wp plugin for SEO").name, "wordpress")
        self.assertEqual(
            detect_language("Gutenberg block for image galleries").name, "wordpress"
        )

    def test_does_not_eat_unrelated(self):
        # Plain "press" / "post" / "blog" should NOT route to WordPress.
        self.assertNotEqual(detect_language("a press kit website").name, "wordpress")
        self.assertNotEqual(detect_language("a blog post manager").name, "wordpress")


class TestBrowserExtensionLanguage(unittest.TestCase):
    def test_shape(self):
        lang = browser_extension_language()
        self.assertEqual(lang.name, "browser_extension")
        self.assertEqual(lang.family, "node")
        self.assertEqual(lang.entry_point, "manifest.json")
        self.assertEqual(lang.test_cmd, ["npx", "vitest", "run"])
        # Quality content present
        self.assertIn("manifest_version", lang.coding_standards)
        self.assertIn("service_worker", lang.project_structure)
        # MV3 antipattern set should mention either eval-class hazards
        # (remote scripts / eval / Function) or service-worker lifecycle.
        ap = lang.anti_patterns.lower()
        self.assertTrue(
            "remote script" in ap or "eval" in ap or "service worker" in ap,
            f"expected an MV3-specific antipattern in {ap!r}",
        )

    def test_detection(self):
        self.assertEqual(
            detect_language("Build a Chrome extension that counts tabs").name,
            "browser_extension",
        )
        self.assertEqual(
            detect_language("an MV3 browser extension for ad blocking").name,
            "browser_extension",
        )
        self.assertEqual(
            detect_language("Chrome plugin to highlight text").name,
            "browser_extension",
        )

    def test_does_not_eat_react(self):
        # "extension" alone in unrelated contexts shouldn't match.
        # Plain React app routes to react.
        self.assertEqual(detect_language("a React + Vite SPA").name, "react")
        # "file extension" / "extensible" must not trigger.
        self.assertNotEqual(
            detect_language("Build a file extension parser library").name,
            "browser_extension",
        )

    def test_browser_extension_priority_over_react(self):
        # "Chrome extension built with React" must route to browser_extension —
        # different toolchain (manifest.json, service worker), not just a React SPA.
        lang = detect_language("Build a Chrome extension using React")
        self.assertEqual(lang.name, "browser_extension")


class TestPhpFamilyDispatchesCleanly(unittest.TestCase):
    """Validation hooks must skip cleanly for the new php family."""

    def test_check_lint_skipped(self):
        from cadillac.validate import check_lint
        lang = wordpress_language()
        results = check_lint("/tmp/nonexistent-ws", lang)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed)

    def test_check_imports_skipped(self):
        from cadillac.validate import check_imports
        lang = wordpress_language()
        results = check_imports("/tmp/nonexistent-ws", lang)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed)

    def test_check_framework_conflicts_skipped(self):
        from cadillac.validate import check_framework_conflicts
        lang = wordpress_language()
        results = check_framework_conflicts("/tmp/nonexistent-ws", lang=lang)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed)

    def test_check_static_names_skipped(self):
        from cadillac.validate import check_static_names
        lang = wordpress_language()
        results = check_static_names("/tmp/nonexistent-ws", lang)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed)


if __name__ == "__main__":
    unittest.main()
