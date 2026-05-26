"""Runtime verification — drive real user flows against the built artifact.

The orchestrator picks one strategy based on language family and project
shape, runs it, and returns a `VerificationResult`. Engine code doesn't
case-switch on strategy — it just consumes the result.

Dispatch chain (first match wins):
  1. HTTP backend detected (Flask/FastAPI/Express)  → http
  2. Static site / browser-ext / wordpress           → skip (no surface)
  3. Interactive (curses/pygame)                     → skip (smoke_run covers)
  4. Runnable binary/CLI entry, no HTTP listener     → cli
  5. Public API surface, no entry runner             → library
  6. Otherwise                                       → skip

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
    return {"http": http_run, "cli": cli_run, "library": library_run}


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

    # (1) Static / extension / wordpress — nothing to drive end-to-end here.
    if family == "static" or name in ("wordpress", "browser_extension"):
        label = f"{name} ({family})" if name and name != family else (name or family)
        return ("skip", f"static surface — {label} project, no runtime to verify")

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

    # (3) HTTP backend present?
    if family in ("python", "node"):
        try:
            from cadillac.validate import _wiring_detect_backend
            if _wiring_detect_backend(workspace) is not None:
                return ("http", "http backend detected")
        except Exception:
            pass

    # (4) CLI — runnable entry point with no HTTP listener.
    if family in ("python", "compiled"):
        if _has_cli_entry(workspace, lang):
            return ("cli", "CLI entry detected")

    # (5) Library — public API surface, no runner.
    if _has_library_surface(workspace, lang):
        return ("library", "library API surface detected")

    return ("skip", "no runtime surface detected")


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
