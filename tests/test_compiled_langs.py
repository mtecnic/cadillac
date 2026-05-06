"""Tests for Go + Rust language support (Phase 2 of the weakness roadmap)."""

import unittest

from cadillac.languages import (
    detect_language,
    go_language,
    python_language,
    react_language,
    rust_language,
)


class TestGoLanguage(unittest.TestCase):
    def test_shape(self):
        lang = go_language()
        self.assertEqual(lang.name, "go")
        self.assertEqual(lang.family, "compiled")
        self.assertEqual(lang.entry_point, "main.go")
        self.assertEqual(lang.package_file, "go.mod")
        self.assertEqual(lang.test_cmd, ["go", "test", "./..."])
        # Quality content present
        self.assertIn("gofmt", lang.coding_standards)
        self.assertIn("cmd/", lang.project_structure)
        self.assertIn("panic", lang.anti_patterns)

    def test_detect_golang_keyword(self):
        # The literal "go" alone is too ambiguous (matches "go to"), so we
        # require word-boundary keywords like "golang" / "go cli" / "go.mod".
        self.assertEqual(detect_language("Build a Go cli for log parsing").name, "go")
        self.assertEqual(detect_language("golang microservice with sqlite").name, "go")
        self.assertEqual(detect_language("a go module that exposes goroutines").name, "go")

    def test_go_does_not_eat_unrelated_tasks(self):
        # "go to" / "ago" / "good" must NOT trigger Go.
        self.assertNotEqual(detect_language("Build a tool to go through logs").name, "go")
        self.assertNotEqual(detect_language("a long-ago feature").name, "go")
        self.assertNotEqual(detect_language("a good Python library").name, "go")


class TestRustLanguage(unittest.TestCase):
    def test_shape(self):
        lang = rust_language()
        self.assertEqual(lang.name, "rust")
        self.assertEqual(lang.family, "compiled")
        self.assertEqual(lang.entry_point, "src/main.rs")
        self.assertEqual(lang.package_file, "Cargo.toml")
        self.assertEqual(lang.test_cmd, ["cargo", "test"])
        self.assertIn("cargo fmt", lang.coding_standards)
        self.assertIn("Cargo.toml", lang.project_structure)
        self.assertIn("unwrap", lang.anti_patterns)

    def test_detect_rust_keyword(self):
        self.assertEqual(detect_language("Build a Rust CLI tool").name, "rust")
        self.assertEqual(detect_language("a tokio web server").name, "rust")
        self.assertEqual(detect_language("crate with serde for JSON").name, "rust")

    def test_rust_does_not_eat_unrelated_tasks(self):
        # "trust" / "thrust" must NOT trigger.
        self.assertNotEqual(detect_language("a trust scoring system").name, "rust")
        self.assertNotEqual(detect_language("a thrust-vector calculator").name, "rust")

    def test_rust_priority_over_python(self):
        # "rust web service with tokio" must route to rust even if it
        # mentions web/service words that the Python branch could grab.
        lang = detect_language("A rust web service using tokio")
        self.assertEqual(lang.name, "rust")


class TestCompiledLangsDoNotBreakOthers(unittest.TestCase):
    def test_python_still_routes(self):
        # Sanity: adding compiled-language routing must not interfere with
        # Python detection. "Flask app" is Python, not Go (despite the
        # word "go" if it appeared).
        self.assertEqual(detect_language("Build a Flask REST API").name, "python")
        self.assertEqual(
            detect_language("a Flask app that aggregates logs").name, "python"
        )

    def test_react_still_routes(self):
        self.assertEqual(detect_language("a React + Vite SPA").name, "react")


if __name__ == "__main__":
    unittest.main()
