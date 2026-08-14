"""Playwright runtime verification — headless-browser probes for any web surface.

Covers React / Vue / Angular / vanilla-HTML / PWA / canvas games. Boots the
project's dev server (vite / ng serve) or a static file server, launches a
headless Chromium via Playwright, and drives generated user flows: goto,
click, fill, key, wait, screenshot, plus assertions on selector visibility,
text, and absence of console errors.

Catches the class of bug that syntax / lint / unit tests can't see:
  - React app compiles but the root component crashes at mount time
  - Phaser canvas mounts but the first-frame update() throws
  - Router link goes 404 because the SPA fallback isn't configured
  - Uncaught promise rejection in an event handler nothing exercised
  - PWA service worker fails to register (silent regression)

Skips gracefully when Playwright isn't installed — the skip is treated as
a "no verifier available" outcome, never a failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field

from .types import Probe, ProbeFailure, VerificationResult


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BrowserStep:
    """One browser action + optional assertion.

    Actions:
      - goto(url)                        — navigate; url may be relative to base
      - click(selector)                  — click an element
      - fill(selector, value)            — type into an input
      - key(value)                       — press a key on the focused/body element
      - wait_ms(value)                   — sleep N ms (for animations, game loops)
      - wait_for(selector)               — wait up to timeout for a selector
      - expect_visible(selector)         — assert selector matches ≥1 visible node
      - expect_hidden(selector)          — assert selector does NOT match visibly
      - expect_text(selector, value)     — assert innerText contains value
      - expect_url(value)                — assert location.href contains value
      - screenshot(name)                 — capture PNG to .cadillac/screens/<name>.png
      - eval(value)                      — run JS in page (asserts truthy result)

    `value` is action-specific: URL, text, keycode, ms, or JS expression.
    Substitution via `{name}` is applied to `selector`, `value`, and `url`
    against captures from prior steps' `capture` field.
    """
    action: str
    selector: str = ""
    value: str = ""
    timeout_ms: int = 5000
    capture: dict[str, str] = field(default_factory=dict)  # {name: "innerText" | "value" | "attr:href"}


@dataclass(frozen=True)
class BrowserFlow(Probe):
    """A chain of browser steps proving one story's acceptance criteria."""
    steps: tuple[BrowserStep, ...] = ()


# ── LLM-driven generation ────────────────────────────────────────────────────


_GENERATE_SYSTEM_PROMPT = """You are a senior QA engineer writing executable \
Playwright browser flows for an autobuilder. Given a spec of user stories and \
a summary of the built page (route paths, prominent selectors, form fields, \
canvas targets), output flows that prove each story actually works in a real \
headless browser.

Output JSON only — no prose, no fences. Schema:

{
  "flows": [
    {
      "story_id": "S03",
      "title": "user adds a habit and marks it complete",
      "priority": "must",
      "steps": [
        {"action": "goto", "value": "/"},
        {"action": "wait_for", "selector": "input[name='habit']"},
        {"action": "fill", "selector": "input[name='habit']", "value": "Read"},
        {"action": "click", "selector": "button:has-text('Add')"},
        {"action": "expect_visible", "selector": "li:has-text('Read')"},
        {"action": "click", "selector": "li:has-text('Read') button"},
        {"action": "expect_text", "selector": ".streak", "value": "1"}
      ]
    }
  ]
}

Rules:
- One flow per must- and should-priority story. Skip "could".
- The first step of a flow is USUALLY `goto`. For SPA apps use relative paths \
(`"/"`, `"/dashboard"`). The runner supplies the base URL (localhost dev server).
- Prefer role/text/label selectors over brittle nth-of-type CSS:
    - `button:has-text('Save')` — Playwright text-match
    - `input[name='email']` — attribute selector
    - `[data-testid='score']` — if the app has data-testid attributes
    - `role=button[name='Add']` — Playwright role selector
  Avoid: `div > div > span:nth-child(3)`, absolute XPaths, class names that \
look like Tailwind hashes.
- For canvas / game surfaces, you cannot inspect canvas pixels. Instead:
    - Assert the canvas element exists: `{"action": "expect_visible", "selector": "canvas"}`
    - Drive input via `key` (e.g. `{"action": "key", "value": "ArrowRight"}`)
    - Give the game loop time to react: `{"action": "wait_ms", "value": "300"}`
    - Assert an on-screen HUD updated: `{"action": "expect_text", "selector": ".score", "value": "1"}`
    - If there's no HUD, use `eval` to read game state: `{"action": "eval", "value": "window.game && window.game.player.x > 100"}`
- Use `wait_for` before asserting on elements that appear async (fetched data, \
route transitions). Never sleep-then-assert when you can wait-for the element.
- `expect_text` uses substring match, case-insensitive. Assert on the \
smallest observable text that proves the state change.
- `key` value is a Playwright key name: `ArrowLeft`, `Space`, `Enter`, `Escape`, \
letter keys as-is (`a`, `A`).
- A trailing `{"action": "screenshot", "value": "story_name"}` on a flow is \
optional and useful for debugging, but not required.
- 5-15 flows for typical apps. Don't pad.

Output ONLY the JSON object."""


def _build_generate_prompt(spec, workspace: str, base_url: str, lang) -> str:
    """Compose the user message summarizing the built page + spec stories."""
    surface = _summarize_web_surface(workspace)
    name = getattr(lang, "name", "?") if lang else "?"
    spec_block = spec.to_prompt_block(max_chars=3500) if spec else ""
    return (
        f"Stack: {name}. Dev server base URL: {base_url}\n\n"
        f"## PAGE SURFACE (statically extracted)\n{surface}\n\n"
        f"{spec_block}\n\n"
        "Generate flows."
    )


_WEB_SURFACE_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "dist", "build",
    "venv", ".venv", ".cadillac",
}


def _summarize_web_surface(workspace: str) -> str:
    """Extract routes + selectors so the LLM writes flows that hit real targets.

    Best-effort static scan — grabs:
      - React Router `<Route path="...">` entries
      - Vue Router entries
      - Angular Route path entries
      - HTML `<a href="...">` links (static sites)
      - Form input `name` / `id` / `placeholder` attributes
      - Elements with `data-testid=` (highest-signal selector shape)
      - Presence of `<canvas>` (signals a game / drawing surface)
      - Registration of a service worker (signals PWA — flows should verify offline)
    """
    routes: set[str] = set()
    testids: set[str] = set()
    input_names: set[str] = set()
    input_placeholders: set[str] = set()
    has_canvas = False
    has_sw = False

    def _visit(text: str) -> None:
        nonlocal has_canvas, has_sw
        for m in re.finditer(r'<Route\s+[^>]*path\s*=\s*["\']([^"\']+)', text):
            routes.add(m.group(1))
        for m in re.finditer(r'path\s*:\s*["\']([/\w:\-]+)["\']', text):
            routes.add(m.group(1))
        for m in re.finditer(r'<a\s+[^>]*href\s*=\s*["\']([^"\']+)', text):
            href = m.group(1)
            if href.startswith("/") or href.startswith("./"):
                routes.add(href)
        for m in re.finditer(r'data-testid\s*=\s*["\']([^"\']+)', text):
            testids.add(m.group(1))
        for m in re.finditer(r'<input\s+[^>]*name\s*=\s*["\']([^"\']+)', text):
            input_names.add(m.group(1))
        for m in re.finditer(r'placeholder\s*=\s*["\']([^"\']+)', text):
            input_placeholders.add(m.group(1))
        if re.search(r'<canvas\b', text, re.IGNORECASE):
            has_canvas = True
        if re.search(r'serviceWorker\.register\s*\(', text):
            has_sw = True

    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _WEB_SURFACE_SKIP_DIRS]
        for fn in files:
            if not fn.endswith((".html", ".tsx", ".jsx", ".ts", ".js",
                                ".vue", ".svelte")):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path) as f:
                    text = f.read()
            except OSError:
                continue
            _visit(text)

    parts: list[str] = []
    if routes:
        parts.append("routes: " + ", ".join(sorted(routes)[:20]))
    if testids:
        parts.append("data-testid: " + ", ".join(sorted(testids)[:20]))
    if input_names:
        parts.append("input names: " + ", ".join(sorted(input_names)[:15]))
    if input_placeholders:
        parts.append("placeholders: " + ", ".join(sorted(input_placeholders)[:10]))
    if has_canvas:
        parts.append("canvas: present (game/drawing surface — use key/wait_ms flows)")
    if has_sw:
        parts.append("service worker: registered (PWA — verify at least one flow "
                     "after `wait_ms 300` reaches an offline-ready state)")
    if not parts:
        return "(no routes or testids extracted — flows should use goto('/') and "\
               "form-name selectors)"
    return "\n".join(parts)


def generate_flows(spec, workspace: str, base_url: str, lang, cfg, emit
                    ) -> list[BrowserFlow]:
    """Call the LLM to produce BrowserFlow objects. Returns a list (may be empty)."""
    from cadillac.engine import chat, extract_json

    if spec is None or spec.is_empty():
        return []
    user_msg = _build_generate_prompt(spec, workspace, base_url, lang)
    messages = [
        {"role": "system", "content": _GENERATE_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    emit("log", msg="[RUNTIME/playwright] generating flows from spec + page surface...")
    try:
        msg = chat(cfg, messages, tools=[], emit=emit)
    except Exception as e:
        emit("log", msg=f"[RUNTIME/playwright] LLM error: {e}")
        return []
    raw = (msg.get("content") or "").strip()
    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        return []
    raw_flows = parsed.get("flows") or []
    if not isinstance(raw_flows, list):
        return []
    flows: list[BrowserFlow] = []
    for entry in raw_flows:
        if not isinstance(entry, dict):
            continue
        f = _flow_from_dict(entry)
        if f is not None:
            flows.append(f)

    try:
        cad_dir = os.path.join(workspace, ".cadillac")
        os.makedirs(cad_dir, exist_ok=True)
        with open(os.path.join(cad_dir, "browser_flows.json"), "w") as fh:
            json.dump([_flow_to_dict(fl) for fl in flows], fh, indent=2)
    except OSError:
        pass

    emit("log", msg=f"[RUNTIME/playwright] {len(flows)} flow(s) generated")
    return flows


_VALID_ACTIONS = frozenset({
    "goto", "click", "fill", "key", "wait_ms", "wait_for",
    "expect_visible", "expect_hidden", "expect_text", "expect_url",
    "screenshot", "eval",
})


def _flow_from_dict(d: dict) -> BrowserFlow | None:
    try:
        story_id = str(d.get("story_id", "")).strip()
        title = str(d.get("title", "")).strip()
        priority = str(d.get("priority", "must")).strip()
        if priority not in ("must", "should", "could"):
            priority = "must"
        raw_steps = d.get("steps") or []
        if not story_id or not isinstance(raw_steps, list):
            return None
        steps: list[BrowserStep] = []
        for s in raw_steps:
            if not isinstance(s, dict):
                continue
            action = str(s.get("action", "")).strip()
            if action not in _VALID_ACTIONS:
                continue
            steps.append(BrowserStep(
                action=action,
                selector=str(s.get("selector", "")),
                value=str(s.get("value", "")),
                timeout_ms=int(s.get("timeout_ms", 5000) or 5000),
                capture=s.get("capture") if isinstance(s.get("capture"), dict) else {},
            ))
        if not steps:
            return None
        return BrowserFlow(story_id=story_id, title=title, priority=priority,
                           steps=tuple(steps))
    except (TypeError, ValueError):
        return None


def _flow_to_dict(f: BrowserFlow) -> dict:
    return {
        "story_id": f.story_id, "title": f.title, "priority": f.priority,
        "steps": [
            {
                "action": s.action, "selector": s.selector, "value": s.value,
                "timeout_ms": s.timeout_ms, "capture": s.capture,
            } for s in f.steps
        ],
    }


# ── Playwright availability check ────────────────────────────────────────────


def playwright_available() -> bool:
    """True when the `playwright` Python package + a chromium browser are usable.

    Import-only. The runner also probes for the browser binary before boot,
    since `pip install playwright` doesn't fetch the browser — `playwright
    install chromium` is a separate step.
    """
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _chromium_installed() -> bool:
    """Heuristic: playwright caches chromium under ~/.cache/ms-playwright/."""
    cache = os.path.expanduser("~/.cache/ms-playwright")
    if not os.path.isdir(cache):
        return False
    try:
        for entry in os.listdir(cache):
            if entry.startswith("chromium"):
                return True
    except OSError:
        pass
    return False


# ── Server boot (dev server or static file server) ───────────────────────────


def _find_free_port(default: int = 5173) -> int:
    for p in (default, default + 1, default + 2, default + 3, 0):
        s = socket.socket()
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", p))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    return default


def _detect_serve_cmd(workspace: str, lang) -> tuple[list[str] | None, str, int, str]:
    """Pick how to serve the built app. Returns (cmd, cwd, port, serve_kind).

    Order:
      1. If a node project with `vite` in devDeps → `npx vite --port N --host 127.0.0.1`
      2. Node project with `ng` (Angular) → `npx ng serve --port N`
      3. HTML static site with index.html → `python3 -m http.server N`
      4. None → nothing to serve; runner should skip.
    """
    from cadillac.validate import _find_node_project_dir

    node_dir = _find_node_project_dir(workspace)
    pkg_path = os.path.join(node_dir, "package.json")
    port = _find_free_port(5173)
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path) as f:
                pkg = json.load(f)
        except (OSError, json.JSONDecodeError):
            pkg = {}
        deps = {**(pkg.get("dependencies") or {}),
                **(pkg.get("devDependencies") or {})}
        if "vite" in deps:
            return (
                ["npx", "--no-install", "vite",
                 "--port", str(port), "--host", "127.0.0.1", "--strictPort"],
                node_dir, port, "vite",
            )
        if "@angular/core" in deps or "@angular/cli" in deps:
            return (
                ["npx", "--no-install", "ng", "serve",
                 "--port", str(port), "--host", "127.0.0.1"],
                node_dir, port, "angular",
            )
        # Fall through: node project without vite/ng — try `npm start` if defined
        scripts = pkg.get("scripts") or {}
        if "start" in scripts:
            return (
                ["npm", "start", "--", "--port", str(port)],
                node_dir, port, "npm-start",
            )

    # Static site — need index.html at workspace root or a well-known subdir
    for rel in ("", "public", "static", "www", "dist"):
        cand = os.path.join(workspace, rel) if rel else workspace
        if os.path.isfile(os.path.join(cand, "index.html")):
            return (
                [sys.executable, "-m", "http.server", str(port),
                 "--bind", "127.0.0.1"],
                cand, port, "static",
            )
    return (None, workspace, 0, "")


def _boot_server(workspace: str, lang, emit
                 ) -> tuple[subprocess.Popen | None, int, str, str]:
    """Start the appropriate dev/static server. Returns (proc, port, kind, err)."""
    cmd, cwd, port, kind = _detect_serve_cmd(workspace, lang)
    if cmd is None:
        return (None, 0, "", "no serving strategy: no vite/angular/index.html found")

    env = os.environ.copy()
    env["PORT"] = str(port)
    env["BROWSER"] = "none"  # prevent CRA-style auto-open

    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        return (None, 0, kind, f"executable not found: {e}")
    except Exception as e:
        return (None, 0, kind, f"failed to launch server: {e}")

    # Dev servers (especially ng serve + first-run vite) can take a while
    # to compile before binding. Give them a generous budget.
    deadline_s = 60 if kind == "angular" else 30
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return (proc, port, kind, "")
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
        if proc.poll() is not None:
            tail = ""
            try:
                if proc.stdout:
                    tail = proc.stdout.read() or ""
            except Exception:
                pass
            return (None, 0, kind,
                    f"server exited before listening (rc={proc.returncode}): "
                    f"{tail[-1500:]}")
    _kill_group(proc)
    return (None, 0, kind, f"server never bound within {deadline_s}s")


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=2)
        except (ProcessLookupError, PermissionError, OSError,
                subprocess.TimeoutExpired):
            pass
    finally:
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass


# ── Substitution + flow execution ────────────────────────────────────────────


def _substitute(template, captures: dict):
    if isinstance(template, str):
        def repl(m):
            key = m.group(1)
            if key in captures:
                return str(captures[key])
            return m.group(0)
        return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, template)
    if isinstance(template, dict):
        return {k: _substitute(v, captures) for k, v in template.items()}
    if isinstance(template, list):
        return [_substitute(v, captures) for v in template]
    return template


def run_flow(page, flow: BrowserFlow, base_url: str, screens_dir: str,
             console_errors: list, emit) -> ProbeFailure | None:
    """Execute one BrowserFlow. Returns the first failure or None.

    `page` is a sync playwright Page. `console_errors` is a shared list that
    the caller populates from page.on('pageerror', ...) and
    page.on('console', ...) — we check it after each step so a JS runtime
    error attributes to the step that triggered it.
    """
    captures: dict = {}
    prior_console_len = len(console_errors)
    for idx, step in enumerate(flow.steps):
        selector = _substitute(step.selector, captures)
        value = _substitute(step.value, captures)
        action = step.action
        summary = f"{action}({selector or value})"[:80]

        try:
            if action == "goto":
                url = value if value.startswith("http") else base_url + (
                    value if value.startswith("/") else "/" + value)
                page.goto(url, timeout=step.timeout_ms, wait_until="load")
            elif action == "click":
                page.click(selector, timeout=step.timeout_ms)
            elif action == "fill":
                page.fill(selector, value, timeout=step.timeout_ms)
            elif action == "key":
                page.keyboard.press(value or "Enter")
            elif action == "wait_ms":
                try:
                    ms = int(value)
                except ValueError:
                    ms = 500
                page.wait_for_timeout(ms)
            elif action == "wait_for":
                page.wait_for_selector(selector, timeout=step.timeout_ms,
                                        state="visible")
            elif action == "expect_visible":
                loc = page.locator(selector).first
                loc.wait_for(state="visible", timeout=step.timeout_ms)
            elif action == "expect_hidden":
                loc = page.locator(selector).first
                try:
                    loc.wait_for(state="visible", timeout=500)
                    return ProbeFailure(
                        probe=flow, failure_kind="element_not_found",
                        detail=f"step {idx + 1} ({summary}): "
                               f"{selector!r} should be hidden but is visible",
                        actual="",
                    )
                except Exception:
                    pass  # not visible — good
            elif action == "expect_text":
                loc = page.locator(selector).first
                loc.wait_for(state="visible", timeout=step.timeout_ms)
                got = (loc.inner_text(timeout=step.timeout_ms) or "").strip()
                if value.lower() not in got.lower():
                    return ProbeFailure(
                        probe=flow, failure_kind="text_mismatch",
                        detail=f"step {idx + 1} ({summary}): "
                               f"element {selector!r} text {got!r} does not "
                               f"contain {value!r}",
                        actual=got[:400],
                    )
            elif action == "expect_url":
                current = page.url
                if value.lower() not in current.lower():
                    return ProbeFailure(
                        probe=flow, failure_kind="text_mismatch",
                        detail=f"step {idx + 1} ({summary}): url {current!r} "
                               f"does not contain {value!r}",
                        actual=current,
                    )
            elif action == "screenshot":
                name = value or f"{flow.story_id}_{idx}"
                safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:60] or "shot"
                page.screenshot(path=os.path.join(screens_dir, f"{safe}.png"))
            elif action == "eval":
                result = page.evaluate(value)
                if not result:
                    return ProbeFailure(
                        probe=flow, failure_kind="assertion",
                        detail=f"step {idx + 1} ({summary}): eval returned "
                               f"falsy: {result!r}",
                        actual=repr(result)[:200],
                    )
        except Exception as e:
            # Playwright raises TimeoutError, TargetClosedError, etc.
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            kind = "element_not_found" if action in (
                "click", "fill", "wait_for", "expect_visible", "expect_text"
            ) else "assertion"
            return ProbeFailure(
                probe=flow, failure_kind=kind,
                detail=f"step {idx + 1} ({summary}): {msg[:200]}",
                actual=str(e)[:400],
            )

        # After each step, check for uncaught JS errors accumulated since the
        # previous checkpoint. Attribute the failure to THIS step.
        if len(console_errors) > prior_console_len:
            new_errs = console_errors[prior_console_len:]
            prior_console_len = len(console_errors)
            return ProbeFailure(
                probe=flow, failure_kind="console_error",
                detail=f"step {idx + 1} ({summary}): page emitted console "
                       f"error(s): {new_errs[0][:150]}",
                actual="\n".join(new_errs)[:600],
            )

        # Capture values from selector (post-step, pre-next-step)
        if step.capture:
            for cap_name, cap_expr in step.capture.items():
                try:
                    if cap_expr in ("innerText", "text"):
                        val = page.locator(selector).first.inner_text(
                            timeout=1000)
                    elif cap_expr in ("value",):
                        val = page.locator(selector).first.input_value(
                            timeout=1000)
                    elif cap_expr.startswith("attr:"):
                        attr = cap_expr[len("attr:"):]
                        val = page.locator(selector).first.get_attribute(attr)
                    else:
                        val = None
                    if val is not None:
                        captures[cap_name] = val
                except Exception:
                    pass
    return None


# ── Strategy entry point ─────────────────────────────────────────────────────


def run(spec, workspace: str, lang, cfg, contract, emit) -> VerificationResult:
    """Playwright strategy entry point. Returns VerificationResult."""
    if not playwright_available():
        return VerificationResult(
            strategy="playwright", probes_run=0,
            skipped_reason=(
                "playwright not installed — run "
                "`pip install --break-system-packages playwright && "
                "playwright install chromium`"
            ),
        )
    if not _chromium_installed():
        return VerificationResult(
            strategy="playwright", probes_run=0,
            skipped_reason=(
                "playwright installed but no chromium binary — run "
                "`playwright install chromium`"
            ),
        )

    proc, port, kind, err = _boot_server(workspace, lang, emit)
    if proc is None:
        # Boot failure without any flows to attach it to → return with a
        # synthetic probe so the engine has something to display.
        emit("log", msg=f"[RUNTIME/playwright] server boot failed: {err}")
        return VerificationResult(
            strategy="playwright", probes_run=0,
            failures=(ProbeFailure(
                probe=Probe(story_id="boot", title=f"{kind or 'server'} boot",
                             priority="must"),
                failure_kind="boot_failure",
                detail=f"could not serve app for browser probing: {err}",
                actual=err,
            ),) if err and "no serving strategy" not in err else (),
            skipped_reason=err if "no serving strategy" in err else "",
        )

    base_url = f"http://127.0.0.1:{port}"
    emit("log", msg=f"[RUNTIME/playwright] serving via {kind} at {base_url}")

    flows = generate_flows(spec, workspace, base_url, lang, cfg, emit)
    if not flows:
        _kill_group(proc)
        return VerificationResult(
            strategy="playwright", probes_run=0,
            skipped_reason="no flows generated",
        )

    # Set up a screenshots directory next to persisted flows.
    screens_dir = os.path.join(workspace, ".cadillac", "screens")
    try:
        os.makedirs(screens_dir, exist_ok=True)
    except OSError:
        pass

    failures: list[ProbeFailure] = []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _kill_group(proc)
        return VerificationResult(
            strategy="playwright", probes_run=0,
            skipped_reason="playwright import failed at run time",
        )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            console_errors: list[str] = []

            def _on_console(m):
                if m.type == "error":
                    console_errors.append(f"[console.{m.type}] {m.text}")

            def _on_pageerror(err):
                console_errors.append(f"[pageerror] {err}")

            page.on("console", _on_console)
            page.on("pageerror", _on_pageerror)

            for flow in flows:
                emit("log",
                     msg=f"[RUNTIME/playwright] flow {flow.story_id}: {flow.title}")
                fail = run_flow(page, flow, base_url, screens_dir,
                                console_errors, emit)
                if fail is not None:
                    emit("log",
                         msg=f"  [fail] {fail.failure_kind} — {fail.detail}")
                    failures.append(fail)

            try:
                context.close()
                browser.close()
            except Exception:
                pass
    except Exception as e:
        emit("log", msg=f"[RUNTIME/playwright] driver crashed: {e}")
        failures.append(ProbeFailure(
            probe=flows[0], failure_kind="boot_failure",
            detail=f"playwright driver crashed: {e}",
            actual=str(e)[:600],
        ))
    finally:
        _kill_group(proc)

    return VerificationResult(
        strategy="playwright",
        probes_run=len(flows),
        failures=tuple(failures),
    )
