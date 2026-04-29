"""Building-code enforcement for Cadillac builds.

Inspections run at phase boundaries to catch three classes of bug the LLM
otherwise ships:

  1. Materials — unpinned or host-incompatible dependency versions.
  2. Wiring    — missing scripts, orphan configs, entry_point that doesn't exist.
  3. Commissioning — entry point that won't load / won't respond to --test.

Each tier returns a list of Violation objects (data, not log lines). A renderer
produces an LLM-readable block to inject into the BUILD/INTEGRATE context.

Guiding principles:
  - One module owns policy. tools.py / engine.py / validate.py consume it.
  - Cheap when clean (sub-100ms when everything is fine).
  - Defense-in-depth with the existing _protect_* self-healing helpers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field


# ─── Data shapes ─────────────────────────────────────────────────────────────

@dataclass
class Violation:
    rule: str          # stable id for tests/logs — e.g. "dep_unpinned"
    severity: str      # "error" | "warning"
    file: str          # path within the workspace
    fix: str           # one-line LLM-actionable instruction
    detail: str = ""   # supporting evidence (truncated to ~500 chars)


# ─── Policy ──────────────────────────────────────────────────────────────────

# Host-safe version ceilings, per Node major. Several npm packages (vitest v2+,
# vite v7+) require newer Node and crash on import on older hosts. We detect
# the actual Node major at startup and pick the right ceilings, so a host
# upgrade (Node 18 → 20 → 22) automatically relaxes the pins without code
# edits. The Node 18 row mirrors memory lesson #12.
_VERSIONS_BY_NODE_MAJOR: dict[int, dict[str, str]] = {
    18: {
        "vitest": "^1",
        "vite": "^5",
        "@vitejs/plugin-react": "^4",
        "@vitejs/plugin-vue": "^4",
        "jest": "^29",
        "ts-jest": "^29",
        "@types/jest": "^29",
    },
    20: {
        "vitest": "^2",
        "vite": "^6",
        "@vitejs/plugin-react": "^4",
        "@vitejs/plugin-vue": "^5",
        "jest": "^29",
        "ts-jest": "^29",
        "@types/jest": "^29",
    },
    22: {
        "vitest": "^3",
        "vite": "^7",
        "@vitejs/plugin-react": "^5",
        "@vitejs/plugin-vue": "^5",
        "jest": "^30",
        "ts-jest": "^29",
        "@types/jest": "^29",
    },
}

# Fallback when Node isn't installed / probe fails. Keeps pre-existing
# Node-18-safe behavior — no regression on hosts we can't inspect.
_VERSIONS_DEFAULT = _VERSIONS_BY_NODE_MAJOR[18]

_host_node_major_cache: int | None = None
_host_versions_cache: dict[str, str] | None = None


def _detect_node_major() -> int | None:
    """Run `node --version` once, parse the major, return it. None on failure.

    Cached for the life of the process — we never expect a Node upgrade
    mid-build.
    """
    global _host_node_major_cache
    if _host_node_major_cache is not None:
        return _host_node_major_cache
    try:
        r = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        m = re.match(r"v?(\d+)\.", r.stdout.strip())
        if not m:
            return None
        _host_node_major_cache = int(m.group(1))
        return _host_node_major_cache
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None


def approved_versions_for_host() -> dict[str, str]:
    """Return the version-ceiling table matching the current host.

    Chooses the largest known-safe Node major that is ≤ the host's major, so
    Node 21 falls back to the 20 row and Node 19 falls back to 18. Cached.
    """
    global _host_versions_cache
    if _host_versions_cache is not None:
        return _host_versions_cache
    major = _detect_node_major()
    if major is None:
        _host_versions_cache = dict(_VERSIONS_DEFAULT)
        return _host_versions_cache
    # Pick the highest supported row ≤ host major.
    supported = sorted(m for m in _VERSIONS_BY_NODE_MAJOR if m <= major)
    row = _VERSIONS_BY_NODE_MAJOR[supported[-1]] if supported else _VERSIONS_DEFAULT
    _host_versions_cache = dict(row)
    return _host_versions_cache


class _ApprovedVersionsProxy:
    """Dict-like view so legacy `APPROVED_VERSIONS[x]` / `in` calls still work
    while the real lookup happens through the host-aware helper."""

    def __getitem__(self, key):
        return approved_versions_for_host()[key]

    def get(self, key, default=None):
        return approved_versions_for_host().get(key, default)

    def __contains__(self, key):
        return key in approved_versions_for_host()

    def __iter__(self):
        return iter(approved_versions_for_host())

    def keys(self):
        return approved_versions_for_host().keys()

    def items(self):
        return approved_versions_for_host().items()


APPROVED_VERSIONS = _ApprovedVersionsProxy()

# Packages that MUST be pinned (no "*", no "latest"). Superset of the
# approved list. Entries without an approved mapping get flagged without a
# specific coercion suggestion. Uses the Node-18 row as the stable superset —
# every newer row covers the same package names.
CRITICAL_PACKAGES: frozenset = frozenset(_VERSIONS_DEFAULT) | frozenset({
    "webpack", "rollup", "esbuild", "ava", "mocha", "tap",
})

# Known npm test-runner package names (used by wiring check).
_NPM_TEST_RUNNERS = ("vitest", "jest", "mocha", "ava", "tap")


# ─── Tier 0 helper — version coercion (called by add_dep gate) ───────────────

def coerce_version(pkg: str, version: str) -> tuple[str, bool]:
    """Map an unsafe version string to the approved pin.

    Returns (coerced_version, was_coerced). Non-critical packages pass through.
    """
    v = (version or "").strip()
    approved = APPROVED_VERSIONS.get(pkg)
    # Strict-unsafe: "*", "", "latest", or "x" wildcards
    is_loose = v in ("", "*", "latest", "x", "X") or re.match(r"^\d+\.x", v) or v.startswith(">=")
    if approved:
        if is_loose or v != approved:
            # Only coerce when clearly unsafe; leave narrower user-chosen ranges
            # alone (e.g. "^1.2.3" when approved is "^1").
            if is_loose:
                return approved, True
            # If the user wrote a major version above our ceiling, coerce.
            m_user = re.match(r"[\^~]?(\d+)", v)
            m_approved = re.match(r"[\^~]?(\d+)", approved)
            if m_user and m_approved and int(m_user.group(1)) > int(m_approved.group(1)):
                return approved, True
        return v, False
    # No approved ceiling for this package — leave as-is.
    return v, False


# ─── Tier 1 — Materials ──────────────────────────────────────────────────────

def inspect_materials(workspace: str, lang) -> list[Violation]:
    """Check dependency versions against policy.

    Currently Node-family only; Python's ecosystem has no analogous
    host-version cliff for the packages we touch.
    """
    out: list[Violation] = []
    if not lang or lang.family != "node":
        return out
    pkg_path = os.path.join(workspace, "package.json")
    if not os.path.exists(pkg_path):
        return out
    try:
        with open(pkg_path) as f:
            pkg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [Violation(
            rule="package_json_parse",
            severity="error",
            file="package.json",
            fix="Restore package.json to valid JSON.",
            detail=str(e)[:200],
        )]
    for section in ("dependencies", "devDependencies"):
        deps = pkg.get(section) or {}
        for name, version in deps.items():
            if name not in CRITICAL_PACKAGES:
                continue
            coerced, was_coerced = coerce_version(name, version)
            if was_coerced:
                out.append(Violation(
                    rule="dep_unpinned",
                    severity="error",
                    file="package.json",
                    fix=(
                        f"Change {section}.{name} from '{version}' to "
                        f"'{coerced}'. Use add_dep('{name}', '{coerced}'"
                        f"{', dev=True' if section == 'devDependencies' else ''})."
                    ),
                    detail=(
                        f"{name} {version!r} is unsafe on this host "
                        f"(Node 18). Approved: {coerced}"
                    ),
                ))
    return out


# ─── Tier 2 — Wiring ─────────────────────────────────────────────────────────

def _has_test_files_node(workspace: str) -> bool:
    for root, _, files in os.walk(workspace):
        if "node_modules" in root or "/.git" in root:
            continue
        for f in files:
            if f.endswith((".test.ts", ".test.tsx", ".test.js", ".test.jsx",
                           ".spec.ts", ".spec.tsx", ".spec.js")):
                return True
    return False


def _installed_runners(pkg: dict) -> list[str]:
    all_deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    return [r for r in _NPM_TEST_RUNNERS if r in all_deps]


def inspect_wiring(workspace: str, lang, plan: dict | None) -> list[Violation]:
    """Check project is self-consistent — scripts, configs, entry points."""
    out: list[Violation] = []
    if not lang:
        return out
    # plan.entry_point exists
    if isinstance(plan, dict):
        entry = plan.get("entry_point")
        if entry and isinstance(entry, str):
            if not os.path.exists(os.path.join(workspace, entry)):
                out.append(Violation(
                    rule="entry_point_missing",
                    severity="error",
                    file=entry,
                    fix=f"Create {entry} as the project entry point.",
                    detail=f"plan.entry_point = {entry!r} but file doesn't exist.",
                ))

    if lang.family != "node":
        return out

    pkg_path = os.path.join(workspace, "package.json")
    if not os.path.exists(pkg_path):
        return out
    try:
        with open(pkg_path) as f:
            pkg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return out

    scripts = pkg.get("scripts") or {}
    runners = _installed_runners(pkg)

    # Missing scripts.test when test files exist
    if _has_test_files_node(workspace):
        if "test" not in scripts:
            if runners:
                runner = runners[0]
                cmd = {
                    "vitest": "vitest run",
                    "jest": "jest --passWithNoTests",
                    "mocha": "mocha --recursive",
                    "ava": "ava",
                    "tap": "tap",
                }.get(runner, f"{runner} run")
                fix = (
                    f"Add \"test\": \"{cmd}\" to package.json scripts. "
                    f"You installed {runner}; wire it as the test command."
                )
            else:
                fix = (
                    "Install a test runner (add_dep('vitest', '^1', dev=True) "
                    "or similar) AND set scripts.test to run it."
                )
            out.append(Violation(
                rule="missing_test_script",
                severity="error",
                file="package.json",
                fix=fix,
                detail="Test files exist but no scripts.test is defined.",
            ))

    # scripts.start references a real file (when it references one)
    start = scripts.get("start", "")
    m = re.search(r"(?:ts-node|tsx|node)\s+(\S+)", start) if start else None
    if m:
        path = m.group(1)
        # Strip leading ./ for existence check
        real = os.path.join(workspace, path.lstrip("./"))
        if not os.path.exists(real):
            out.append(Violation(
                rule="start_script_missing_file",
                severity="error",
                file="package.json",
                fix=(
                    f"scripts.start points to {path} which does not exist. "
                    f"Either create it or update the script."
                ),
                detail=f"scripts.start = {start!r}",
            ))

    # tsconfig include globs aren't empty (TS only)
    if lang.name == "typescript" or lang.family == "node":
        tsconfig_path = os.path.join(workspace, "tsconfig.json")
        if os.path.exists(tsconfig_path):
            try:
                with open(tsconfig_path) as f:
                    tsc = json.load(f)
                include = tsc.get("include")
                if include == []:
                    out.append(Violation(
                        rule="tsconfig_empty_include",
                        severity="warning",
                        file="tsconfig.json",
                        fix=(
                            "Add an include glob like [\"src/**/*\", "
                            "\"**/*.test.ts\"] to tsconfig.json."
                        ),
                        detail="include: [] excludes every file from type-checking.",
                    ))
            except (OSError, json.JSONDecodeError):
                pass

    return out


# ─── Tier 3 — Commissioning ──────────────────────────────────────────────────

def _static_collection_check(workspace: str, lang) -> Violation | None:
    """The old _workspace_collection_check logic, promoted into inspector.

    Runs pytest --collect-only (Python) or tsc --noEmit (plain TS). Surfaces
    cross-module import/type errors that per-module tests miss.
    """
    if not lang:
        return None
    if lang.family == "python":
        try:
            r = subprocess.run(
                ["python3", "-m", "pytest", "--collect-only", "-q"],
                cwd=workspace, capture_output=True, text=True, timeout=45,
            )
        except Exception as e:
            return None  # skip on tooling errors
        if r.returncode in (0, 5):  # 5 = no tests collected (ok for us)
            return None
        combined = (r.stdout + "\n" + r.stderr).strip()[-2000:]
        return Violation(
            rule="collection_error",
            severity="error",
            file="(workspace)",
            fix="Fix the cross-module import/type error listed in detail.",
            detail=combined,
        )
    if lang.family == "node" and (not lang.name or lang.name not in ("react", "vue", "angular", "electron")):
        try:
            r = subprocess.run(
                ["npx", "tsc", "--noEmit"],
                cwd=workspace, capture_output=True, text=True, timeout=60,
            )
        except Exception:
            return None
        if r.returncode == 0:
            return None
        combined = (r.stdout + "\n" + r.stderr).strip()[-2000:]
        return Violation(
            rule="collection_error",
            severity="error",
            file="(workspace)",
            fix="Fix the TypeScript compile error listed in detail.",
            detail=combined,
        )
    return None


_RUNTIME_ERROR_PATTERNS = re.compile(
    r"(ModuleNotFoundError|ImportError|SyntaxError|TypeError|"
    r"ReferenceError|error TS\d+|AssertionError)"
)


def inspect_commissioning(workspace: str, lang, plan: dict | None) -> list[Violation]:
    """Static cross-workspace check. A runtime smoke would be nice but it's
    risky in the general case (may start servers, open sockets, etc.), so the
    commissioning tier currently delegates to the static checks only. Runtime
    smoke is left to the VALIDATE phase which has proper timeout + sandboxing.
    """
    out: list[Violation] = []
    stat = _static_collection_check(workspace, lang)
    if stat:
        out.append(stat)
    return out


# ─── LLM-readable rendering ──────────────────────────────────────────────────

def verify_language_against_plan(lang, plan: dict | None):
    """Tier 0 — Blueprint verification. Returns a (maybe-downgraded) lang.

    If `lang.name == "react"` but the plan's files list shows no evidence of
    React (no .tsx files, no JSX, no react/react-dom in plan.dependencies),
    this downgrades to plain typescript so the boilerplate doesn't pollute
    the project with vite+@vitejs/plugin-react+testing-library for a plain
    Node CLI that happened to match a heuristic keyword.

    Returns the lang unchanged if no correction needed, or a new downgraded
    lang when correction applies. Callers should reassign: lang = verify_...(lang, plan).
    """
    from .languages import typescript_language
    if lang is None or not isinstance(plan, dict):
        return lang
    if getattr(lang, "name", "") != "react":
        return lang
    files = plan.get("files") or []
    deps = plan.get("dependencies") or []
    # Evidence of React usage
    has_tsx = any(
        isinstance(f, dict) and str(f.get("path", "")).endswith((".tsx", ".jsx"))
        for f in files
    )
    has_react_dep = any(
        isinstance(d, str) and (d == "react" or d.startswith("react-") or d == "react-dom")
        for d in deps
    )
    has_jsx_interface = any(
        isinstance(f, dict) and any(
            "JSX" in s or "Component" in s or "useState" in s or "useEffect" in s
            for s in (f.get("interfaces") or [])
        )
        for f in files
    )
    if has_tsx or has_react_dep or has_jsx_interface:
        return lang  # genuine React project
    return typescript_language()


def render_for_llm(violations: list[Violation], run_cmd: str = "") -> str:
    """Produce a single "BUILDING-CODE VIOLATIONS" block for injection.

    Returns empty string if the list is empty.
    """
    if not violations:
        return ""
    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity == "warning"]
    parts = ["BUILDING-CODE VIOLATIONS (fix these before proceeding):"]
    for v in errors:
        line = f"  [{v.rule}] {v.file}: {v.fix}"
        if v.detail:
            line += f"\n    > {v.detail[:300]}"
        parts.append(line)
    if warnings:
        parts.append("warnings (non-blocking):")
        for v in warnings:
            parts.append(f"  [{v.rule}] {v.file}: {v.fix}")
    return "\n".join(parts)
