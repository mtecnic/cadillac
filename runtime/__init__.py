"""Runtime verification — drive real user flows against the built artifact.

The orchestrator picks one strategy based on language family and project
shape, runs it, and returns a `VerificationResult`. Engine code doesn't
case-switch on strategy — it just consumes the result.

Dispatch chain (first match wins):
  1. Browser-ext / wordpress                         → skip (no headless surface)
  2. Interactive (curses/pygame)                     → skip (smoke_run covers)
  3. MCP server (imports mcp SDK)                    → mcp (JSON-RPC over stdio)
  4. Browser-served surface (React/Vue/Angular/HTML) → playwright (headless Chromium)
  5. HTTP backend detected (Flask/FastAPI/Express)   → http
  6. Runnable binary/CLI entry, no HTTP listener     → cli
  7. Public API surface, no entry runner             → library
  8. Otherwise                                       → skip

MCP dispatches BEFORE http and cli because MCP servers have a runnable
entry (looks like CLI) but the correct probe uses JSON-RPC 2.0 over
stdio, not argv. Getting this wrong produces 100% "false failure"
noise on real MCP builds (measured on the 2026-07-02 build).

Playwright dispatches BEFORE http because a full-stack build has both a
frontend AND a backend — the browser probe exercises the whole stack
(user clicks, XHR fires, server responds, UI updates), which is a
strictly stronger signal than hitting the API alone. For backend-only
builds with no served UI, playwright's serve-cmd detection returns None
and dispatch falls through to http.

Skip is always graceful — never blocks a build that has no surface to verify.
"""

from __future__ import annotations

from .format import format_for_iterate
from .types import (
    FAILURE_KINDS,
    Probe,
    ProbeFailure,
    VerificationResult,
)


__all__ = [
    "FAILURE_KINDS",
    "Probe",
    "ProbeFailure",
    "VerificationResult",
    "format_for_iterate",
    "runtime_verify",
]


# ── Strategy registry ────────────────────────────────────────────────────────
#
# Populated lazily inside `runtime_verify` to avoid module-import cycles with
# engine.py (which imports this package). Each runner exports a single entry
# function `run(spec, workspace, lang, cfg, contract, emit) -> VerificationResult`.


def _load_runners() -> dict[str, callable]:
    from .http_runner import run as http_run
    from .cli_runner import run as cli_run
    from .library_runner import run as library_run
    from .mcp_runner import run as mcp_run
    from .playwright_runner import run as playwright_run
    return {"http": http_run, "cli": cli_run, "library": library_run,
            "mcp": mcp_run, "playwright": playwright_run}


# ── Dispatch ─────────────────────────────────────────────────────────────────


def _pick_strategy(workspace: str, lang) -> tuple[str, str]:
    """Decide which strategy (if any) to run. Returns (strategy, reason).

    `reason` is informational — included in the VerificationResult when
    strategy=="skip" so the build log explains *why* nothing ran.
    """
    if lang is None:
        return ("skip", "no language detected")

    family = getattr(lang, "family", "")
    name = getattr(lang, "name", "")

    # (1) Extensions / wordpress — no headless surface. Browser extensions
    # would need `--load-extension` in a chromium launch args; possible but
    # non-trivial and out of scope for the general web-runtime pass.
    if name in ("wordpress", "browser_extension"):
        return ("skip", f"no headless surface — {name} project")

    # (2) Interactive (curses/pygame) — `check_smoke_run` already exercises
    # the entry path with a synthetic screen. Driving real flows would need
    # input synthesis we don't have.
    if family == "python":
        try:
            from cadillac.validate import _detect_interactive_framework
            if _detect_interactive_framework(workspace):
                return ("skip", "interactive (curses/pygame) — smoke_run covers entry")
        except Exception:
            pass

    # (3) MCP server — imports the mcp SDK and exposes a stdio server.
    # Must come BEFORE http/cli because an MCP server looks like both
    # (has a main.py entry) but neither probe is correct for it. The mcp
    # runner speaks JSON-RPC 2.0 over stdin/stdout.
    if family == "python" and _detect_mcp_server(workspace):
        return ("mcp", "MCP server detected (imports mcp SDK)")

    # (4) Browser-served surface — React/Vue/Angular/HTML/static PWA/game.
    # Must come BEFORE http, because a full-stack build has BOTH a frontend
    # and a backend; the browser probe drives the whole loop (click → XHR
    # → server → DOM update) which is a strictly stronger check than the
    # bare-JSON http probe. If nothing web-servable is present, this falls
    # through to (5)/http.
    if _detect_web_surface(workspace, lang):
        return ("playwright", "browser-served surface detected")

    # (5) HTTP backend present?
    if family in ("python", "node"):
        try:
            from cadillac.validate import _wiring_detect_backend
            if _wiring_detect_backend(workspace) is not None:
                return ("http", "http backend detected")
        except Exception:
            pass

    # (6) CLI — runnable entry point with no HTTP listener.
    if family in ("python", "compiled"):
        if _has_cli_entry(workspace, lang):
            return ("cli", "CLI entry detected")

    # (7) Library — public API surface, no runner.
    if _has_library_surface(workspace, lang):
        return ("library", "library API surface detected")

    return ("skip", "no runtime surface detected")


def _detect_web_surface(workspace: str, lang) -> bool:
    """True when the project has a browser-servable UI.

    Recognizes:
      - Node projects with `vite` or Angular deps (dev server available)
      - Static sites with an index.html at the workspace root or in
        public/ / static/ / www/ / dist/
    """
    import json
    import os

    family = getattr(lang, "family", "")
    name = getattr(lang, "name", "")

    # Explicit web-family languages that ship a served UI.
    if name in ("react", "vue", "angular", "html", "electron"):
        # Electron: skip (its UI lives inside BrowserWindow, not a served
        # dev server we can hit from playwright's chromium).
        if name == "electron":
            return False
        return True

    # A served frontend can live in a subdir even when the outer language
    # is Python (full-stack Flask + React/Vue in `frontend/`). Family-agnostic
    # scan of the well-known frontend directories.
    for candidate in (workspace, os.path.join(workspace, "frontend"),
                       os.path.join(workspace, "client"),
                       os.path.join(workspace, "web"),
                       os.path.join(workspace, "ui")):
        pkg = os.path.join(candidate, "package.json")
        if not os.path.isfile(pkg):
            continue
        try:
            with open(pkg) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        deps = {**(data.get("dependencies") or {}),
                **(data.get("devDependencies") or {})}
        if any(k in deps for k in ("vite", "@angular/core",
                                    "@angular/cli", "next", "svelte")):
            return True

    # Static: any index.html at root or a well-known subdir
    for rel in ("", "public", "static", "www", "dist"):
        cand = os.path.join(workspace, rel) if rel else workspace
        if os.path.isfile(os.path.join(cand, "index.html")):
            return True

    return False


def _detect_mcp_server(workspace: str) -> bool:
    """True when any Python file in the workspace imports the mcp SDK.

    Recognizes both spellings:
        import mcp
        from mcp import ...
        from mcp.server.fastmcp import FastMCP
        from mcp.server import Server
    """
    import os
    import re
    skip = {"node_modules", ".git", "__pycache__", "dist", "build",
            "venv", ".venv", ".cadillac", "frontend"}
    pattern = re.compile(r"^\s*(?:from|import)\s+mcp(?:\b|\.)",
                          re.MULTILINE)
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path) as f:
                    text = f.read()
            except OSError:
                continue
            if pattern.search(text):
                return True
    return False


def _has_cli_entry(workspace: str, lang) -> bool:
    """True when the project has a runnable CLI but no HTTP server."""
    import os
    family = getattr(lang, "family", "")
    if family == "python":
        # Python CLI: main.py with __main__ guard + argparse / sys.argv usage,
        # but NO Flask/FastAPI imports anywhere (those would be caught in
        # step 3 above; if we reach here that already failed).
        entry_candidates = ["main.py", "cli.py", "src/main.py"]
        for rel in entry_candidates:
            path = os.path.join(workspace, rel)
            if os.path.isfile(path):
                try:
                    with open(path) as f:
                        text = f.read()
                except OSError:
                    continue
                if "__main__" in text and ("argparse" in text or "sys.argv" in text):
                    return True
        return False
    if family == "compiled":
        # Rust: needs an actual bin target. Cargo.toml alone isn't enough —
        # a lib-only crate would otherwise dispatch as CLI.
        cargo = os.path.join(workspace, "Cargo.toml")
        if os.path.isfile(os.path.join(workspace, "src/main.rs")):
            return True
        if os.path.isfile(cargo):
            try:
                with open(cargo) as f:
                    cargo_text = f.read()
                if "[[bin]]" in cargo_text:
                    return True
            except OSError:
                pass
        # Go: main.go with package main
        for root, _dirs, files in os.walk(workspace):
            for fn in files:
                if not fn.endswith(".go"):
                    continue
                try:
                    with open(os.path.join(root, fn)) as f:
                        head = f.read(500)
                    if "package main" in head:
                        return True
                except OSError:
                    continue
        return False
    return False


def _has_library_surface(workspace: str, lang) -> bool:
    """True when project looks like a library — public exports, no runner."""
    import os
    family = getattr(lang, "family", "")
    if family == "python":
        # At least one package directory with __init__.py and re-exports,
        # AND no main.py/cli.py with a runner.
        has_pkg = False
        for entry in os.listdir(workspace):
            init = os.path.join(workspace, entry, "__init__.py")
            if os.path.isfile(init):
                try:
                    with open(init) as f:
                        text = f.read()
                    if "from ." in text or "__all__" in text:
                        has_pkg = True
                        break
                except OSError:
                    continue
        has_runner = any(
            os.path.isfile(os.path.join(workspace, c))
            for c in ("main.py", "cli.py")
        )
        return has_pkg and not has_runner
    if family == "node":
        # package.json with "main" or "exports" but no "bin" entry.
        pkg_path = os.path.join(workspace, "package.json")
        if os.path.isfile(pkg_path):
            import json
            try:
                with open(pkg_path) as f:
                    pkg = json.load(f)
                has_main = bool(pkg.get("main") or pkg.get("exports"))
                has_bin = bool(pkg.get("bin"))
                return has_main and not has_bin
            except (OSError, json.JSONDecodeError):
                return False
    if family == "compiled":
        # Cargo.toml [lib] section, no [[bin]].
        cargo = os.path.join(workspace, "Cargo.toml")
        if os.path.isfile(cargo):
            try:
                with open(cargo) as f:
                    text = f.read()
                return "[lib]" in text and "[[bin]]" not in text
            except OSError:
                return False
    return False


# ── Public entry point ───────────────────────────────────────────────────────


def runtime_verify(spec, workspace: str, lang, cfg, contract=None,
                    emit=None) -> VerificationResult:
    """Run runtime verification against the built artifact.

    Returns a `VerificationResult`. Never raises — strategy errors degrade
    to a skip with the reason captured.
    """
    if emit is None:
        emit = lambda *a, **kw: None

    strategy, reason = _pick_strategy(workspace, lang)
    if strategy == "skip":
        emit("log", msg=f"[RUNTIME] skipped — {reason}")
        return VerificationResult(strategy="skip", probes_run=0,
                                   skipped_reason=reason)

    if spec is None or spec.is_empty():
        emit("log", msg="[RUNTIME] no spec stories to verify, skipping")
        return VerificationResult(strategy=strategy, probes_run=0,
                                   skipped_reason="empty spec")

    emit("log", msg=f"[RUNTIME] strategy={strategy} ({reason})")
    try:
        runners = _load_runners()
        return runners[strategy](spec, workspace, lang, cfg, contract, emit)
    except Exception as e:
        emit("log", msg=f"[RUNTIME] runner crashed (advisory): {e}")
        return VerificationResult(strategy="skip", probes_run=0,
                                   skipped_reason=f"runner crashed: {e}")
