"""Tests for the Playwright runtime runner + dispatch, PWA-manifest check,
asset-reference check, and the web-game / PWA quality addendum.

Three layers:
  1. Dispatch — _detect_web_surface + _pick_strategy correctly route web
     projects to the playwright strategy without regressing existing
     mcp/http/cli/library dispatch.
  2. Pure functions — surface summary, flow parsing, substitution.
  3. Static checks — check_pwa_manifest and check_asset_refs on
     synthesized workspaces covering the happy and failing paths.

The integration layer (real playwright + real dev server) is intentionally
NOT here — it would require playwright + chromium installed on the test
host, and it exercises third-party code we don't own. The runner's
`playwright_available()` / `_chromium_installed()` gates handle the skip
gracefully at run time.
"""

from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest

from cadillac import quality
from cadillac.languages import (
    detect_language,
    html_language,
    react_language,
    python_language,
    vue_language,
    angular_language,
    electron_language,
    wordpress_language,
    browser_extension_language,
)
from cadillac.runtime import _detect_web_surface, _pick_strategy
from cadillac.runtime.playwright_runner import (  # noqa: F401 — imported for smoke
    BrowserFlow,
    BrowserStep,
    _flow_from_dict,
    _substitute,
    _summarize_web_surface,
)
from cadillac.validate import (
    check_asset_refs,
    check_pwa_manifest,
    _looks_like_pwa_manifest,
)


def _mkfile(ws: str, rel: str, content: str) -> None:
    full = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(full) or ws, exist_ok=True)
    with open(full, "w") as f:
        f.write(textwrap.dedent(content))


# ─────────────────────── Web-surface detection ───────────────────────


class TestDetectWebSurface(unittest.TestCase):
    def test_html_language_always_a_web_surface(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertTrue(_detect_web_surface(ws, html_language()))

    def test_react_language_always_a_web_surface(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertTrue(_detect_web_surface(ws, react_language()))

    def test_vue_language_always_a_web_surface(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertTrue(_detect_web_surface(ws, vue_language()))

    def test_angular_language_always_a_web_surface(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertTrue(_detect_web_surface(ws, angular_language()))

    def test_electron_is_not_a_playwright_surface(self):
        """Electron's UI is in a BrowserWindow, not a served dev URL."""
        with tempfile.TemporaryDirectory() as ws:
            self.assertFalse(_detect_web_surface(ws, electron_language()))

    def test_node_project_with_vite_dep_is_a_web_surface(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "package.json", json.dumps({
                "name": "app", "devDependencies": {"vite": "^5"}
            }))
            from cadillac.languages import typescript_language
            self.assertTrue(_detect_web_surface(ws, typescript_language()))

    def test_static_site_with_index_html_is_a_web_surface(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html", "<!DOCTYPE html><html></html>")
            self.assertTrue(_detect_web_surface(ws, python_language()))

    def test_public_index_html_counts(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "public/index.html", "<!DOCTYPE html>")
            self.assertTrue(_detect_web_surface(ws, python_language()))

    def test_pure_python_no_html_is_not_a_web_surface(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "main.py", "print('hi')")
            self.assertFalse(_detect_web_surface(ws, python_language()))


# ─────────────────────── Dispatch chain ───────────────────────


class TestPlaywrightDispatch(unittest.TestCase):
    def test_react_project_picks_playwright(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "package.json", json.dumps({
                "name": "app", "dependencies": {"react": "^18"},
                "devDependencies": {"vite": "^5"},
            }))
            _mkfile(ws, "index.html", "<!DOCTYPE html>")
            strategy, reason = _pick_strategy(ws, react_language())
            self.assertEqual(strategy, "playwright",
                              f"expected playwright, got {strategy!r} ({reason})")

    def test_html_static_site_picks_playwright(self):
        """Previously html/static family was skipped — now dispatches to browser."""
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html", "<!DOCTYPE html><body></body></html>")
            strategy, _ = _pick_strategy(ws, html_language())
            self.assertEqual(strategy, "playwright")

    def test_wordpress_still_skipped(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "plugin.php", "<?php // Plugin Name: X ?>")
            strategy, _ = _pick_strategy(ws, wordpress_language())
            self.assertEqual(strategy, "skip")

    def test_browser_extension_still_skipped(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "manifest.json", json.dumps({"manifest_version": 3,
                                                       "name": "x"}))
            strategy, _ = _pick_strategy(ws, browser_extension_language())
            self.assertEqual(strategy, "skip")

    def test_python_flask_backend_still_picks_http(self):
        """A Flask backend with no served frontend must still dispatch to http."""
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "app.py",
                    "from flask import Flask\napp = Flask(__name__)\n"
                    "if __name__ == '__main__':\n    app.run(port=5000)\n")
            strategy, _ = _pick_strategy(ws, python_language())
            self.assertEqual(strategy, "http")

    def test_python_cli_still_picks_cli(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "main.py",
                    "import argparse\n"
                    "if __name__ == '__main__':\n"
                    "    argparse.ArgumentParser().parse_args()\n")
            strategy, _ = _pick_strategy(ws, python_language())
            self.assertEqual(strategy, "cli")

    def test_fullstack_prefers_playwright_over_http(self):
        """When both a Flask backend AND a React frontend exist, playwright
        wins — it drives the whole loop; http alone hits the API in isolation."""
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "app.py",
                    "from flask import Flask\napp = Flask(__name__)\n"
                    "if __name__ == '__main__':\n    app.run()\n")
            _mkfile(ws, "frontend/package.json", json.dumps({
                "name": "fe", "devDependencies": {"vite": "^5", "react": "^18"},
            }))
            _mkfile(ws, "frontend/index.html", "<!DOCTYPE html>")
            # Language detects as python (backend-first); but the dispatch
            # sees the served frontend and picks playwright.
            strategy, _ = _pick_strategy(ws, python_language())
            self.assertEqual(strategy, "playwright")


# ─────────────────────── Surface summary ───────────────────────


class TestSummarizeWebSurface(unittest.TestCase):
    def test_extracts_react_routes(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "src/App.jsx", """
                import { Route } from 'react-router-dom';
                <Route path="/dashboard" element={<Dashboard/>}/>
                <Route path="/settings" element={<Settings/>}/>
            """)
            surface = _summarize_web_surface(ws)
            self.assertIn("/dashboard", surface)
            self.assertIn("/settings", surface)

    def test_extracts_data_testid(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "src/App.jsx",
                    '<button data-testid="submit-btn">Save</button>')
            surface = _summarize_web_surface(ws)
            self.assertIn("submit-btn", surface)

    def test_detects_canvas(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html",
                    "<!DOCTYPE html><body><canvas id='g'></canvas></body></html>")
            surface = _summarize_web_surface(ws)
            self.assertIn("canvas: present", surface)

    def test_detects_service_worker(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "src/main.js",
                    "navigator.serviceWorker.register('/sw.js');")
            surface = _summarize_web_surface(ws)
            self.assertIn("service worker", surface.lower())

    def test_extracts_form_inputs(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html",
                    "<input name='email' placeholder='your email'>")
            surface = _summarize_web_surface(ws)
            self.assertIn("email", surface)
            self.assertIn("your email", surface)


# ─────────────────────── Flow parsing ───────────────────────


class TestFlowParsing(unittest.TestCase):
    def test_parses_clean_flow(self):
        f = _flow_from_dict({
            "story_id": "S01", "title": "sign up", "priority": "must",
            "steps": [
                {"action": "goto", "value": "/"},
                {"action": "fill", "selector": "input[name='email']",
                 "value": "a@b.com"},
                {"action": "click", "selector": "button:has-text('Submit')"},
                {"action": "expect_visible", "selector": ".welcome"},
            ],
        })
        self.assertIsNotNone(f)
        self.assertEqual(f.story_id, "S01")
        self.assertEqual(len(f.steps), 4)
        self.assertEqual(f.steps[0].action, "goto")
        self.assertEqual(f.steps[3].action, "expect_visible")

    def test_rejects_unknown_action(self):
        f = _flow_from_dict({
            "story_id": "S01", "title": "x", "priority": "must",
            "steps": [{"action": "hack_the_planet", "value": ""}],
        })
        # Unknown action filtered out → no valid steps → whole flow rejected
        self.assertIsNone(f)

    def test_rejects_empty_steps(self):
        f = _flow_from_dict({
            "story_id": "S01", "title": "x", "priority": "must", "steps": []
        })
        self.assertIsNone(f)

    def test_normalizes_priority(self):
        f = _flow_from_dict({
            "story_id": "S01", "title": "x", "priority": "gibberish",
            "steps": [{"action": "goto", "value": "/"}],
        })
        self.assertEqual(f.priority, "must")


# ─────────────────────── Substitution ───────────────────────


class TestSubstitute(unittest.TestCase):
    def test_string_substitution(self):
        self.assertEqual(_substitute("hello {name}", {"name": "Ada"}),
                          "hello Ada")

    def test_leaves_unknown(self):
        self.assertEqual(_substitute("{missing}", {}), "{missing}")

    def test_dict_recursive(self):
        got = _substitute({"selector": "#{id}"}, {"id": "root"})
        self.assertEqual(got, {"selector": "#root"})


# ─────────────────────── Quality addendum ───────────────────────


class TestWebGameAddendum(unittest.TestCase):
    def test_phaser_task_triggers_addendum(self):
        self.assertTrue(quality.is_web_game_task("build a phaser platformer"))
        self.assertIn("game loop",
                       quality.web_game_addendum("phaser platformer game"))

    def test_pwa_task_triggers_addendum(self):
        self.assertTrue(quality.is_pwa_task(
            "build an installable PWA todo list"))
        self.assertIn("service worker",
                       quality.web_game_addendum("build a PWA"))

    def test_plain_react_task_no_addendum(self):
        self.assertEqual(quality.web_game_addendum("build a react todo list"),
                          "")

    def test_detect_language_wires_addendum_into_react(self):
        """A phaser+react task should ship the addendum in the anti_patterns block."""
        lang = detect_language("build a react phaser arcade game")
        self.assertIn("game loop", lang.anti_patterns.lower())

    def test_detect_language_wires_addendum_into_html(self):
        lang = detect_language(
            "build a static HTML canvas game with vanilla javascript")
        self.assertIn("game loop", lang.anti_patterns.lower())

    def test_detect_language_plain_react_no_addendum(self):
        lang = detect_language("build a react todo list")
        self.assertNotIn("game loop", lang.anti_patterns.lower())


# ─────────────────────── PWA manifest check ───────────────────────


class TestPWAManifestCheck(unittest.TestCase):
    def test_no_manifest_skips(self):
        with tempfile.TemporaryDirectory() as ws:
            results = check_pwa_manifest(ws, python_language())
            self.assertTrue(results[0].passed)
            self.assertEqual(results[0].severity, "info")

    def test_valid_manifest_passes(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "public/icon-192.png", "PNG")
            _mkfile(ws, "public/icon-512.png", "PNG")
            _mkfile(ws, "public/manifest.webmanifest", json.dumps({
                "name": "MyApp", "short_name": "MyApp",
                "start_url": "/", "display": "standalone",
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192",
                     "type": "image/png"},
                    {"src": "/icon-512.png", "sizes": "512x512",
                     "type": "image/png"},
                ],
            }))
            results = check_pwa_manifest(ws, react_language())
            self.assertTrue(results[0].passed, results[0].output)

    def test_missing_icon_file_fails(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "public/manifest.webmanifest", json.dumps({
                "name": "MyApp", "start_url": "/", "display": "standalone",
                "icons": [{"src": "/icons/missing.png", "sizes": "192x192"}],
            }))
            results = check_pwa_manifest(ws, react_language())
            self.assertFalse(results[0].passed)
            self.assertIn("missing.png", results[0].output)

    def test_missing_name_fails(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "public/icon.png", "PNG")
            _mkfile(ws, "public/manifest.webmanifest", json.dumps({
                "start_url": "/", "display": "standalone",
                "icons": [{"src": "/icon.png", "sizes": "192x192"}],
            }))
            results = check_pwa_manifest(ws, react_language())
            self.assertFalse(results[0].passed)
            self.assertIn("name", results[0].output)

    def test_mv3_extension_manifest_ignored(self):
        """A Chrome MV3 manifest.json is NOT a PWA manifest — must be skipped."""
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "manifest.json", json.dumps({
                "manifest_version": 3, "name": "ext", "version": "1.0",
                "background": {"service_worker": "bg.js"},
            }))
            results = check_pwa_manifest(ws, browser_extension_language())
            self.assertTrue(results[0].passed)  # info-skip, no failure

    def test_package_json_ignored(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "manifest.json", json.dumps({
                "name": "pkg", "dependencies": {"react": "^18"},
            }))
            results = check_pwa_manifest(ws, react_language())
            self.assertTrue(results[0].passed)

    def test_invalid_display_value_fails(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "public/icon.png", "PNG")
            _mkfile(ws, "public/manifest.webmanifest", json.dumps({
                "name": "X", "start_url": "/", "display": "fullwindow",
                "icons": [{"src": "/icon.png", "sizes": "192x192"}],
            }))
            results = check_pwa_manifest(ws, react_language())
            self.assertFalse(results[0].passed)
            self.assertIn("display", results[0].output)


class TestPWAManifestClassifier(unittest.TestCase):
    def test_recognizes_web_manifest(self):
        self.assertTrue(_looks_like_pwa_manifest({
            "name": "X", "start_url": "/", "display": "standalone",
        }))

    def test_rejects_mv3(self):
        self.assertFalse(_looks_like_pwa_manifest({
            "manifest_version": 3, "name": "X"
        }))

    def test_rejects_package_json(self):
        self.assertFalse(_looks_like_pwa_manifest({
            "name": "X", "dependencies": {}
        }))

    def test_rejects_random_json(self):
        self.assertFalse(_looks_like_pwa_manifest({
            "meta": "some other file"
        }))


# ─────────────────────── Asset-ref check ───────────────────────


class TestAssetRefsCheck(unittest.TestCase):
    def test_no_html_skips(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "main.py", "print('hi')")
            results = check_asset_refs(ws, python_language())
            self.assertTrue(results[0].passed)
            self.assertIn("skipped", results[0].output.lower())

    def test_valid_relative_ref_passes(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html",
                    "<link href='css/style.css'><img src='img/logo.png'>")
            _mkfile(ws, "css/style.css", "body{}")
            _mkfile(ws, "img/logo.png", "PNG")
            results = check_asset_refs(ws, html_language())
            self.assertTrue(results[0].passed, results[0].output)

    def test_missing_asset_fails(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html", "<img src='img/missing.png'>")
            results = check_asset_refs(ws, html_language())
            self.assertFalse(results[0].passed)
            self.assertIn("missing.png", results[0].output)

    def test_absolute_url_ignored(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html",
                    "<img src='https://example.com/pic.png'>"
                    "<link href='//cdn.example.com/x.css'>")
            results = check_asset_refs(ws, html_language())
            self.assertTrue(results[0].passed)

    def test_data_url_ignored(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html",
                    "<img src='data:image/png;base64,abc'>")
            results = check_asset_refs(ws, html_language())
            self.assertTrue(results[0].passed)

    def test_template_placeholder_ignored(self):
        """`{{icon}}`, `${url}`, `<%= href %>` — dynamic, cannot file-check."""
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html",
                    "<img src='{{iconPath}}.png'>"
                    "<link href='${cssUrl}.css'>")
            results = check_asset_refs(ws, html_language())
            self.assertTrue(results[0].passed)

    def test_route_href_ignored(self):
        """`href='/dashboard'` is a route, not an asset — no extension."""
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html", "<a href='/dashboard'>Go</a>")
            results = check_asset_refs(ws, html_language())
            self.assertTrue(results[0].passed)

    def test_public_lookup_resolves_absolute_ref(self):
        """Vite serves `public/logo.png` at `/logo.png` — check must know that."""
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html", "<img src='/logo.png'>")
            _mkfile(ws, "public/logo.png", "PNG")
            results = check_asset_refs(ws, react_language())
            self.assertTrue(results[0].passed, results[0].output)

    def test_php_project_skipped(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "index.html", "<img src='/nope.png'>")
            results = check_asset_refs(ws, wordpress_language())
            self.assertTrue(results[0].passed)
            self.assertEqual(results[0].severity, "info")


if __name__ == "__main__":
    unittest.main()
