"""Core agent loop with phase state machine, LLM communication, and context management."""

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import requests

from .events import EventEmitter, default_print_handler
from .languages import detect_language
from .manifest import FileManifest, ErrorTracker, BatchTracker
from .memory import (
    recall, format_for_prompt, parse_reflection, save_lesson, boost_confidence,
    load_lessons, Lesson, infer_task_tags, penalize_backfired,
    record_phase_outcome,
)
from .modules import ModularPlan, ModuleSpec, validate_modular_plan, detect_affected_modules, extract_real_interfaces
from .scratch import Scratch
from .phases import Phase, PhaseState, compute_budgets
from .progress import Progress
from .prompts import (
    build_architecture_prompt, build_manifest_prompt,
    build_scaffold_prompt, build_build_prompt,
    build_modular_architecture_prompt, build_modular_manifest_prompt,
    build_module_scaffold_prompt, build_module_build_prompt,
    build_integration_prompt,
    build_analyze_prompt, build_delta_plan_prompt, build_enhance_build_prompt,
    REFLECTION_PROMPT,
)
from .quality import build_review_prompt
from .tools import TOOL_DEFS, ToolExecutor, ModuleScopedExecutor, extract_failure_text
from .validate import run_validation, run_module_validation, format_failures, results_to_dict
from . import inspector


def _pip_install_cmd(dep: str, workspace: str) -> str:
    """Build pip install command, detecting venv and adjusting flags."""
    in_venv = (
        sys.prefix != sys.base_prefix
        or os.environ.get("VIRTUAL_ENV")
        or os.path.exists(os.path.join(workspace, ".venv"))
    )
    if in_venv:
        return f"pip3 install '{dep}'"
    else:
        return f"pip3 install --break-system-packages '{dep}'"


# Packages LLMs commonly include in a plan that are Python-only. Putting them
# in package.json triggers `npm install` → E404 from the registry and blocks
# the whole build. The list covers the ecosystem families cadillac has
# actually seen LLMs mix in; extend as new ones surface.
_PYTHON_ONLY_DEPS: frozenset = frozenset({
    # Web frameworks + extensions
    "flask", "flask-cors", "flask-sqlalchemy", "flask-login", "flask-wtf",
    "flask-migrate", "flask-restful", "flask-jwt-extended",
    "django", "djangorestframework", "django-cors-headers",
    "fastapi", "uvicorn", "starlette", "gunicorn", "waitress",
    # Testing
    "pytest", "pytest-cov", "pytest-asyncio", "pytest-mock", "pytest-flask",
    "pytest-django", "coverage", "hypothesis",
    # ORMs / data
    "sqlalchemy", "alembic", "psycopg2", "psycopg2-binary", "pymysql",
    "aiosqlite", "peewee",
    # HTTP clients / async
    "requests", "httpx", "aiohttp", "websockets",
    # Validation / config
    "pydantic", "marshmallow", "python-dotenv", "pyyaml",
    # CLI
    "click", "typer", "rich", "textual",
    # Scientific
    "numpy", "pandas", "matplotlib", "scipy", "scikit-learn", "tensorflow",
    "torch", "pytorch",
    # Linting / formatting (usually devDependencies)
    "black", "ruff", "mypy", "pylint", "flake8", "isort",
    # Misc
    "pygame", "beautifulsoup4", "lxml", "pillow", "celery",
})


def _partition_deps_by_ecosystem(deps: list[str]) -> tuple[list[str], list[str]]:
    """Split a mixed dependency list into (npm, python) buckets.

    LLM plans for full-stack tasks frequently include both (`"deps": [
    "flask", "axios", "pytest", "react"]`). Writing all to package.json
    causes npm to 404 on Python names; writing all to requirements.txt does
    the mirror. Route each name to the correct manager using the
    curated _PYTHON_ONLY_DEPS set. Unknown names default to npm (caller is
    already in the Node boilerplate path, so npm is the safer default).
    """
    npm: list[str] = []
    python: list[str] = []
    for d in deps:
        if not d:
            continue
        # Normalize to lowercase for set membership; preserve original casing
        # in output so any scoped npm packages @foo/bar stay intact.
        if d.lower() in _PYTHON_ONLY_DEPS:
            python.append(d)
        else:
            npm.append(d)
    return npm, python


def _write_node_boilerplate(workspace: str, deps: list[str], lang,
                            subdir: str = "") -> None:
    """Write package.json, tsconfig.json, and framework config for Node family projects.

    `subdir` routes the boilerplate into a subdirectory (e.g. "frontend") so
    full-stack plans with a dedicated frontend dir get their package.json +
    tsconfig.json + index.html co-located with the React code. Without this,
    we'd write `index.html` at workspace root pointing to `/src/main.tsx`
    while the LLM correctly places `frontend/src/main.tsx`, leaving vite
    with a dangling reference and "Rollup failed to resolve" build failures.
    Empty string means workspace root (legacy behavior).
    """
    base = os.path.join(workspace, subdir) if subdir else workspace
    os.makedirs(base, exist_ok=True)
    pkg_json = os.path.join(base, "package.json")
    tsconfig_path = os.path.join(base, "tsconfig.json")

    # --- Determine framework-specific dev deps and config ---
    _typed_pkgs = {"express", "supertest", "cors", "uuid"}
    dev_deps: dict[str, str] = {"typescript": "*", "@types/node": "*"}
    scripts: dict[str, str] = {}

    if lang.name == "react":
        dev_deps.update({
            "@vitejs/plugin-react": "^4", "vite": "^5",
            "vitest": "^1", "@testing-library/react": "*",
            "@testing-library/jest-dom": "*", "jsdom": "*",
            "@types/react": "*", "@types/react-dom": "*",
        })
        scripts = {
            "dev": "vite", "build": "vite build", "preview": "vite preview",
            "test": "vitest run",
        }
    elif lang.name == "vue":
        dev_deps.update({
            "@vitejs/plugin-vue": "^4", "vite": "^5",
            "vitest": "^1", "@vue/test-utils": "*", "jsdom": "*",
            "vue-tsc": "*",
        })
        scripts = {
            "dev": "vite", "build": "vite build", "preview": "vite preview",
            "test": "vitest run",
        }
    elif lang.name == "angular":
        dev_deps.update({
            "@angular/cli": "*", "@angular/compiler-cli": "*",
            "karma": "*", "karma-chrome-launcher": "*",
            "karma-jasmine": "*",
        })
        scripts = {
            "start": "ng serve", "build": "ng build",
            "test": "ng test --watch=false --browsers=ChromeHeadless",
        }
    else:
        # Plain TypeScript (Express/Node.js backend).
        # Ship only the universal baseline — no hardcoded test runner.
        # The LLM picks jest/vitest/mocha/etc. during SCAFFOLD and writes its own config
        # + sets `scripts.test` via add_dep + its own package.json edit during SCAFFOLD
        # (when config_writes_allowed=True). This avoids the jest/vitest mismatch bug.
        dev_deps.update({
            "ts-node": "*", "tsx": "*",
        })
        scripts = {
            "start": "npx ts-node src/index.ts",
        }

    # Split deps: Python names go to requirements.txt, npm names to package.json.
    # Without this filter, npm install E404s on "flask"/"pytest" and the whole
    # full-stack build stalls before scaffold even finishes.
    npm_deps, python_deps = _partition_deps_by_ecosystem(deps)

    # Add @types/ for known packages (only applies to npm deps).
    for d in npm_deps:
        if d in _typed_pkgs:
            dev_deps[f"@types/{d}"] = "*"

    pkg = {
        "name": "project", "version": "1.0.0", "private": True,
        "dependencies": {d: "*" for d in npm_deps},
        "devDependencies": dev_deps,
        "scripts": scripts,
    }
    # Vite / Vitest / React / Vue / Angular projects are ESM-native. Without
    # "type": "module" at package.json root, Vite prints a loud
    # "CJS build of Vite's Node API is deprecated" warning to stderr on every
    # build invocation. Our own validator picks up that stderr text and
    # flags `run`/`functional` as FAIL, trapping the build in retries even
    # though exit code is 0. Set type:module for ESM ecosystems up front.
    if lang.name in ("react", "vue", "angular"):
        pkg["type"] = "module"
    with open(pkg_json, "w") as f:
        json.dump(pkg, f, indent=2)

    # Write requirements.txt for the Python side of a full-stack plan. If a
    # backend/ subdir exists (full-stack layout), write there so `pip install
    # -r backend/requirements.txt` is the natural command. Otherwise sits at
    # workspace root. Never overwrites an existing file — the LLM or a later
    # scaffold pass may have curated it already.
    if python_deps:
        if os.path.isdir(os.path.join(workspace, "backend")):
            req_path = os.path.join(workspace, "backend", "requirements.txt")
        else:
            req_path = os.path.join(workspace, "requirements.txt")
        if not os.path.exists(req_path):
            with open(req_path, "w") as f:
                for d in python_deps:
                    f.write(f"{d}\n")

    # --- tsconfig.json ---
    if not os.path.exists(tsconfig_path):
        if lang.name == "react":
            tsconfig = {
                "compilerOptions": {
                    "target": "ES2020", "module": "ESNext",
                    "moduleResolution": "bundler", "jsx": "react-jsx",
                    "lib": ["ES2020", "DOM", "DOM.Iterable"],
                    "strict": True, "skipLibCheck": True,
                    "forceConsistentCasingInFileNames": True,
                    "resolveJsonModule": True,
                },
                "include": ["src"],
            }
        elif lang.name == "vue":
            tsconfig = {
                "compilerOptions": {
                    "target": "ES2020", "module": "ESNext",
                    "moduleResolution": "bundler", "jsx": "preserve",
                    "lib": ["ES2020", "DOM", "DOM.Iterable"],
                    "strict": True, "skipLibCheck": True,
                    "resolveJsonModule": True,
                },
                "include": ["src/**/*.ts", "src/**/*.vue"],
            }
        elif lang.name == "angular":
            tsconfig = {
                "compilerOptions": {
                    "target": "ES2022", "module": "ES2022",
                    "moduleResolution": "node", "strict": True,
                    "lib": ["ES2022", "dom"],
                    "skipLibCheck": True, "experimentalDecorators": True,
                    "forceConsistentCasingInFileNames": True,
                },
                "include": ["src"],
            }
        else:
            tsconfig = {
                "compilerOptions": {
                    "target": "es2020", "module": "commonjs",
                    "lib": ["es2020"], "strict": True,
                    "esModuleInterop": True, "skipLibCheck": True,
                    "forceConsistentCasingInFileNames": True,
                    "resolveJsonModule": True,
                    "outDir": "./dist",
                    "declaration": True,
                    "types": ["jest", "node"],
                },
                "include": ["src/**/*", "**/*.test.ts", "**/*.spec.ts"],
                "exclude": ["node_modules", "dist"],
            }
        with open(tsconfig_path, "w") as f:
            json.dump(tsconfig, f, indent=2)

    # --- Framework-specific config files ---
    if lang.name == "react":
        vite_config = os.path.join(base, "vite.config.ts")
        if not os.path.exists(vite_config):
            with open(vite_config, "w") as f:
                f.write("import { defineConfig } from 'vite';\n"
                        "import react from '@vitejs/plugin-react';\n\n"
                        "export default defineConfig({\n  plugins: [react()],\n});\n")
        # Vite env declarations for CSS modules and assets
        vite_env = os.path.join(base, "src", "vite-env.d.ts")
        os.makedirs(os.path.dirname(vite_env), exist_ok=True)
        if not os.path.exists(vite_env):
            with open(vite_env, "w") as f:
                f.write('/// <reference types="vite/client" />\n'
                        'declare module "*.module.css" {\n'
                        '  const classes: { readonly [key: string]: string };\n'
                        '  export default classes;\n}\n'
                        'declare module "*.css" {}\n'
                        'declare module "*.svg" {\n'
                        '  const src: string;\n  export default src;\n}\n'
                        'declare module "*.png" {\n'
                        '  const src: string;\n  export default src;\n}\n'
                        'declare module "*.jpg" {\n'
                        '  const src: string;\n  export default src;\n}\n')
        # index.html for Vite
        index_html = os.path.join(base, "index.html")
        if not os.path.exists(index_html):
            with open(index_html, "w") as f:
                f.write('<!DOCTYPE html>\n<html lang="en">\n<head>\n'
                        '  <meta charset="UTF-8" />\n'
                        '  <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
                        '  <title>App</title>\n</head>\n<body>\n'
                        '  <div id="root"></div>\n'
                        '  <script type="module" src="/src/main.tsx"></script>\n'
                        '</body>\n</html>\n')
    elif lang.name == "vue":
        vite_config = os.path.join(base, "vite.config.ts")
        if not os.path.exists(vite_config):
            with open(vite_config, "w") as f:
                f.write("import { defineConfig } from 'vite';\n"
                        "import vue from '@vitejs/plugin-vue';\n\n"
                        "export default defineConfig({\n  plugins: [vue()],\n});\n")
        index_html = os.path.join(base, "index.html")
        if not os.path.exists(index_html):
            with open(index_html, "w") as f:
                f.write('<!DOCTYPE html>\n<html lang="en">\n<head>\n'
                        '  <meta charset="UTF-8" />\n'
                        '  <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
                        '  <title>App</title>\n</head>\n<body>\n'
                        '  <div id="app"></div>\n'
                        '  <script type="module" src="/src/main.ts"></script>\n'
                        '</body>\n</html>\n')
    elif lang.name == "angular":
        angular_json = os.path.join(workspace, "angular.json")
        if not os.path.exists(angular_json):
            ang_config = {
                "version": 1,
                "projects": {
                    "project": {
                        "root": "", "sourceRoot": "src",
                        "architect": {
                            "build": {
                                "builder": "@angular-devkit/build-angular:application",
                                "options": {
                                    "outputPath": "dist",
                                    "index": "src/index.html",
                                    "browser": "src/main.ts",
                                    "tsConfig": "tsconfig.json",
                                },
                            },
                        },
                    },
                },
            }
            with open(angular_json, "w") as f:
                json.dump(ang_config, f, indent=2)
    # Plain TS: no hardcoded test-runner config. The LLM writes vitest.config.ts,
    # jest.config.js, or whatever it chose during SCAFFOLD (config_writes_allowed=True).


def _protect_tsconfig(workspace: str, lang) -> bool:
    """Validate and restore tsconfig.json critical settings for plain TS projects.

    LLMs frequently overwrite the boilerplate tsconfig, changing module from
    commonjs to ESM variants, which breaks the entire build. This function
    restores the critical compiler options if they were changed.
    Returns True if the tsconfig was restored.
    """
    if not lang or lang.name != "typescript":
        return False
    tsconfig_path = os.path.join(workspace, "tsconfig.json")
    if not os.path.exists(tsconfig_path):
        return False
    try:
        with open(tsconfig_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False

    opts = config.get("compilerOptions", {})
    changed = False

    # Ensure module is commonjs (LLMs change to "Node16", "ESNext", etc.)
    if opts.get("module", "").lower() != "commonjs":
        opts["module"] = "commonjs"
        changed = True

    # Ensure esModuleInterop is on (needed for CJS default imports)
    if not opts.get("esModuleInterop"):
        opts["esModuleInterop"] = True
        changed = True

    # Remove rootDir if it excludes test files (common LLM mistake)
    if "rootDir" in opts:
        del opts["rootDir"]
        changed = True

    # Ensure skipLibCheck is on (avoids type errors in node_modules)
    if not opts.get("skipLibCheck"):
        opts["skipLibCheck"] = True
        changed = True

    # Ensure include covers test files
    include = config.get("include", [])
    has_tests = any("test" in i.lower() or "spec" in i.lower() for i in include)
    if not has_tests:
        include.extend(["**/*.test.ts", "**/*.spec.ts"])
        config["include"] = include
        changed = True

    if changed:
        config["compilerOptions"] = opts
        with open(tsconfig_path, "w") as f:
            json.dump(config, f, indent=2)
    return changed


def _auto_install_missing_deps(workspace: str, failures_text: str, lang, emit) -> list[str]:
    """Detect and install missing npm packages from validation failures.

    Parses "Cannot find module 'X'" errors from tsc/jest output and
    runs npm install for each missing package. Returns list of installed packages.
    """
    if not lang or lang.family != "node":
        return []
    # Match: Cannot find module 'supertest', Cannot find module '@types/ws'
    missing = set(re.findall(r"Cannot find module '([^']+)'", failures_text))
    # Filter out relative imports and node builtins
    from .languages import _NODE_BUILTINS
    missing = {m for m in missing if not m.startswith(".") and not m.startswith("/")
               and m not in _NODE_BUILTINS}
    if not missing:
        return []
    # Also install @types/ for known packages
    to_install = set()
    for pkg in missing:
        to_install.add(pkg)
        base = pkg.split("/")[0].lstrip("@")
        if not pkg.startswith("@types/"):
            to_install.add(f"@types/{base}")
    if to_install:
        cmd = f"npm install --save-dev {' '.join(sorted(to_install))}"
        emit("log", msg=f"[Auto-install] Installing missing deps: {', '.join(sorted(to_install))}")
        from .tools import ToolExecutor
        executor = ToolExecutor(workspace, None)
        result = executor.run_command(cmd)
        if result.get("exit_code", 1) == 0:
            emit("log", msg=f"[Auto-install] OK")
        else:
            emit("log", msg=f"[Auto-install] Failed: {result.get('stderr', '')[:200]}")
    return sorted(to_install)


def _protect_package_json(workspace: str, lang) -> bool:
    """Remove 'type': 'module' from package.json when it conflicts with the
    chosen test runner.

    Context: `"type": "module"` is an anti-pattern for CommonJS-based Jest
    (ts-jest, babel-jest) — jest.config.js breaks, tests fail to load. BUT
    it's required for Vite / vitest / React+Vite projects, where it silences
    the "CJS build of Vite's Node API is deprecated" warning and enables the
    correct ESM code path. This function strips the field only when we detect
    a CJS-style setup; for Vite/ESM ecosystems we preserve it.

    Scan order for full-stack layouts (backend/ + frontend/): the top-level
    package.json is the one we check here; a nested frontend/package.json is
    handled by separate protection passes over that subdir.
    """
    if not lang or lang.family != "node":
        return False
    pkg_path = os.path.join(workspace, "package.json")
    if not os.path.exists(pkg_path):
        return False
    try:
        with open(pkg_path) as f:
            pkg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False

    if pkg.get("type") != "module":
        return False

    # Preserve "type": "module" when the project uses an ESM-first ecosystem.
    # Vite, vitest, and the major SPA frameworks (React/Vue/Angular via Vite)
    # all expect ESM — stripping breaks them.
    all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    uses_esm_runner = any(
        r in all_deps for r in ("vite", "vitest", "@vitejs/plugin-react",
                                 "@vitejs/plugin-vue", "rollup")
    )
    is_spa_framework = lang and lang.name in ("react", "vue", "angular")
    if uses_esm_runner or is_spa_framework:
        return False

    del pkg["type"]
    with open(pkg_path, "w") as f:
        json.dump(pkg, f, indent=2)
        f.write("\n")
    return True


def _strip_js_extensions_from_ts(workspace: str, manifest: "FileManifest | None" = None) -> int:
    """Strip .js extensions from import/export paths in TypeScript files.

    LLMs often generate `from './foo.js'` in TS files, which breaks
    CommonJS resolution. This rewrites them to `from './foo'`.
    Returns the number of files modified.
    """
    _js_import_re = re.compile(
        r"""((?:from|import)\s+['"])(\.\.?/[^'"]*?)(\.js)(['"])""",
    )
    modified = 0
    for root, _dirs, files in os.walk(workspace):
        # Skip node_modules and dist at ANY depth — full-stack projects put
        # node_modules inside frontend/ so a startswith("node_modules") check
        # misses the nested case and pollutes the manifest with 90+ .d.ts
        # files from transitive deps. Substring-match in path components matches
        # every other walk in this codebase.
        if "node_modules" in root.split(os.sep) or "dist" in root.split(os.sep):
            continue
        for fname in files:
            if not fname.endswith((".ts", ".tsx")):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r") as f:
                    content = f.read()
                new_content = _js_import_re.sub(r"\1\2\4", content)
                if new_content != content:
                    with open(fpath, "w") as f:
                        f.write(new_content)
                    modified += 1
                    # Update manifest if available
                    if manifest:
                        rel_path = os.path.relpath(fpath, workspace)
                        manifest.record(rel_path, new_content)
            except Exception:
                pass
    return modified


def _wrote_any_ts_file(msg: dict) -> bool:
    """True if the assistant message contained a write/edit to a .ts or .tsx file."""
    if not isinstance(msg, dict):
        return False
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        if fn.get("name") not in ("write_file", "edit_file", "line_edit", "write_test"):
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        p = args.get("path", "")
        if isinstance(p, str) and (p.endswith(".ts") or p.endswith(".tsx")):
            return True
    return False


def _pinned_versions_block(workspace: str, lang) -> str:
    """Read live dependency pins from package.json / requirements.txt and format
    as an authoritative state block for LLM injection.

    Memory lessons can become stale ("X needs newer Y" when the harness already
    pinned Y to a compatible version). Surfacing the live pins alongside lessons
    lets the LLM reconcile ambition with reality instead of chasing phantom incompat.
    """
    if not lang:
        return ""
    if lang.family == "node":
        pkg_path = os.path.join(workspace, "package.json")
        if not os.path.exists(pkg_path):
            return ""
        try:
            with open(pkg_path) as f:
                pkg = json.load(f)
        except (OSError, json.JSONDecodeError):
            return ""
        all_deps = {
            **(pkg.get("dependencies") or {}),
            **(pkg.get("devDependencies") or {}),
        }
        if not all_deps:
            return ""
        # Cap at 25 entries so prompt stays compact; prioritize critical deps.
        from .inspector import CRITICAL_PACKAGES
        critical = {k: v for k, v in all_deps.items() if k in CRITICAL_PACKAGES}
        other = {k: v for k, v in all_deps.items() if k not in CRITICAL_PACKAGES}
        ordered = list(critical.items()) + list(sorted(other.items()))[:max(0, 25 - len(critical))]
        lines = [
            "## Current Pinned Dependencies (LIVE from package.json)",
            "These are the versions ACTUALLY installed. If past-build lessons conflict with these,",
            "TRUST WHAT'S PINNED. The harness enforces host-compatible ranges.",
        ]
        for name, ver in ordered:
            lines.append(f"  - {name}: {ver}")
        return "\n".join(lines)
    if lang.family == "python":
        req_path = os.path.join(workspace, "requirements.txt")
        if not os.path.exists(req_path):
            return ""
        try:
            with open(req_path) as f:
                lines = [l.strip() for l in f.readlines() if l.strip() and not l.startswith("#")]
        except OSError:
            return ""
        if not lines:
            return ""
        return "## Current Installed Packages (LIVE from requirements.txt)\n" + \
               "\n".join(f"  - {l}" for l in lines[:25])
    return ""


def _inject_plan_constraints(plan: dict, scratch: "Scratch | None", emit) -> list[str]:
    """Extract `constraints` from the plan and append each to the workspace scratch as
    a 'reminder' entry. Scratch is re-injected into every phase's system prompt
    (every 3 rounds in BUILD, on every phase entry), so these constraints become
    persistent ambient context the LLM sees throughout the build.

    Returns the list of constraints accepted (after scratch dedup).
    """
    raw = plan.get("constraints") if isinstance(plan, dict) else None
    if not raw or not isinstance(raw, list):
        return []
    # Accept only non-empty strings. Reject None/int/dict/etc. — an LLM that
    # stuffs nulls into constraints shouldn't pollute the scratch with "None".
    cleaned = [c.strip() for c in raw if isinstance(c, str) and c.strip()]
    if not cleaned:
        return []
    emit("log", msg=f"[CONSTRAINTS] {len(cleaned)} task constraint(s) pinned to scratch")
    for c in cleaned:
        emit("log", msg=f"  - {c[:120]}")
    if scratch is not None:
        for c in cleaned:
            try:
                scratch.append(category="reminder", content=c, phase="PLAN", round_num=0)
            except Exception:
                pass
    return cleaned


def _scratch_budget(max_context_tokens: int | None = None,
                    n_modules: int = 1) -> dict:
    """Context-proportional scratch read caps. Keyed per injection point.

    Problem being solved: fixed caps like `cap_per=1000` / `max_chars=5000` work
    for 40K windows but starve a 131K build of scratch-state visibility. With
    5% of context reserved for scratch content, bigger window → richer context.

    Token→char ratio ~4. `n_modules` > 1 shrinks the per-dep cap so cross-module
    scratch doesn't balloon.

    Returns {"root": ..., "dep": ..., "entry": ...} (all in chars).
    """
    if not max_context_tokens or max_context_tokens <= 0:
        max_context_tokens = 40000  # legacy default
    total_chars = int(max_context_tokens * 0.05 * 4)
    total_chars = min(total_chars, 30_000)  # absolute ceiling
    root = max(1500, total_chars // 4)
    per_dep = max(500, total_chars // max(1, n_modules * 2))
    entry = max(2000, total_chars // 2)
    return {"root": root, "dep": per_dep, "entry": entry}


def _write_python_entry_stub(workspace: str, entry_path: str) -> bool:
    """Write a minimal entry-point stub for a Python project at plan.entry_point.

    Parallel to _write_node_boilerplate: foundation-time — prevents the pattern
    where LLM ignores the `entry_point_missing` warning because it thinks its
    cli/main.py IS the entry. If the file already exists on disk, this is a no-op.

    Returns True if the stub was written.
    """
    if not entry_path:
        return False
    full = os.path.join(workspace, entry_path)
    if os.path.exists(full):
        return False  # already there — LLM may have written it, don't overwrite
    stub = (
        "#!/usr/bin/env python3\n"
        '"""Entry point — declared in plan.entry_point.\n\n'
        "Fill in main() with CLI parsing and dispatch. The --test flag should run\n"
        "pytest (or your own tests) and exit 0/1.\n"
        '"""\n'
        "from __future__ import annotations\n"
        "import sys\n\n\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    argv = sys.argv[1:] if argv is None else argv\n"
        "    # TODO: parse argv, dispatch subcommands / --test / --file\n"
        "    return 0\n\n\n"
        'if __name__ == "__main__":\n'
        "    sys.exit(main())\n"
    )
    os.makedirs(os.path.dirname(full) or workspace, exist_ok=True)
    with open(full, "w") as f:
        f.write(stub)
    return True


def _auto_shim_entry_point(workspace: str, lang, entry_path: str, emit) -> bool:
    """If the declared entry_point is missing but the LLM wrote a CLI main
    elsewhere, auto-generate a 5-line shim at the expected path. Addresses the
    v1-v6 pattern where inspector fires 14x on `entry_point_missing` and the
    LLM ignores it because it thinks `cli/main.py` is the "real" entry.

    Returns True if a shim was written. Python-only for now.
    """
    if not lang or lang.family != "python":
        return False
    if not entry_path:
        return False
    full_path = os.path.join(workspace, entry_path)
    if os.path.exists(full_path):
        return False  # entry_point present; nothing to shim

    # Find the best candidate for the "real" main by scanning source files.
    # Skip __pycache__, tests, __init__.py. Score by directory/filename heuristics.
    candidates = []
    for root, _dirs, files in os.walk(workspace):
        if any(skip in root for skip in ("/__pycache__", "/.cadillac",
                                         "/node_modules", "/.git", "/.venv")):
            continue
        rel_root = os.path.relpath(root, workspace)
        for f in files:
            if not f.endswith(".py") or f == "__init__.py":
                continue
            if f.startswith("test_") or f.endswith("_test.py"):
                continue
            fpath = os.path.join(root, f)
            try:
                with open(fpath) as fp:
                    head = fp.read(4000)
            except OSError:
                continue
            if "def main(" not in head and "def main " not in head:
                continue
            score = 0
            low_rel = rel_root.replace("\\", "/").lower()
            if "cli" in low_rel or "/main" in low_rel or low_rel == "main":
                score += 2
            if f in ("main.py", "cli.py", "app.py"):
                score += 3
            if "if __name__" in head:
                score += 1
            # Compute importable module path
            rel = (rel_root + "/" + f[:-3]).replace(os.sep, "/")
            if rel.startswith("./"):
                rel = rel[2:]
            mod_name = rel.replace("/", ".")
            candidates.append((score, mod_name))

    if not candidates:
        return False
    candidates.sort(key=lambda x: -x[0])
    top_score, mod_name = candidates[0]
    if top_score == 0:
        return False  # nothing confidently identifiable

    shim = (
        "#!/usr/bin/env python3\n"
        '"""Auto-generated entry-point shim.\n\n'
        f"Task declared {entry_path} as the entry point but the LLM placed\n"
        f"main() in {mod_name}. This shim wires them up so the declared\n"
        "interface (`python3 " + entry_path + "`) works as specified.\n"
        '"""\n'
        "import sys\n"
        f"from {mod_name} import main\n\n"
        'if __name__ == "__main__":\n'
        "    result = main()\n"
        "    sys.exit(result if isinstance(result, int) else 0)\n"
    )
    os.makedirs(os.path.dirname(full_path) or workspace, exist_ok=True)
    with open(full_path, "w") as f:
        f.write(shim)
    emit("log", msg=f"[auto-shim] Wrote {entry_path} → delegates to {mod_name}.main()")
    return True


def _workspace_collection_check(workspace: str, lang) -> tuple[bool, str]:
    """Legacy shim — delegates to inspector.inspect_commissioning.

    Kept for backward compatibility with external call sites. New code should
    call inspector directly.
    """
    violations = inspector.inspect_commissioning(workspace, lang, plan=None)
    errors = [v for v in violations if v.severity == "error"]
    if not errors:
        return True, ""
    # Concat details for legacy consumers that want a single string.
    combined = "\n".join(v.detail or v.fix for v in errors)[-2000:]
    return False, combined


def _run_inspector(tier: str, workspace: str, lang, plan, emit,
                   *, messages: list[dict] | None = None) -> list[inspector.Violation]:
    """Run one inspector tier, log results, optionally inject a user message.

    Returns the list of violations (may be empty).
    """
    fn = {
        "materials": inspector.inspect_materials,
        "wiring": lambda w, l: inspector.inspect_wiring(w, l, plan),
        "commissioning": lambda w, l: inspector.inspect_commissioning(w, l, plan),
    }.get(tier)
    if fn is None:
        return []
    violations = fn(workspace, lang)
    if not violations:
        return []
    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity == "warning"]
    emit("log", msg=(
        f"[INSPECTOR {tier}] {len(errors)} error(s), {len(warnings)} warning(s)"
    ))
    for v in errors + warnings:
        emit("log", msg=f"  [{v.rule}] {v.file}: {v.fix[:120]}")
    if messages is not None and errors:
        block = inspector.render_for_llm(violations)
        if block:
            messages.append({"role": "user", "content": block})
    return violations


def _post_round_safeguards(workspace, lang, manifest, messages, msg, emit) -> None:
    """Per-round hygiene for Node/TS builds. Idempotent; safe to call every round.

    Runs _protect_package_json, _protect_tsconfig (defense-in-depth behind the
    executor's config lockdown), and re-strips .js extensions from TS imports
    if any TS file was written this round.
    """
    if not lang or lang.family != "node":
        return
    notes = []
    if _protect_package_json(workspace, lang):
        notes.append("package.json 'type:module' stripped")
    if _protect_tsconfig(workspace, lang):
        notes.append("tsconfig.json critical settings restored")
    if _wrote_any_ts_file(msg):
        n = _strip_js_extensions_from_ts(workspace, manifest)
        if n:
            notes.append(f".js stripped from {n} TS imports")
    if notes:
        emit("log", msg=f"[safeguard] {'; '.join(notes)}")
        messages.append({
            "role": "user",
            "content": (
                "HARNESS SAFEGUARD auto-fixed: " + "; ".join(notes) +
                ". Do NOT re-introduce these patterns."
            ),
        })


@dataclass
class Config:
    api_url: str = field(default_factory=lambda: os.getenv("CADILLAC_API_URL", "http://localhost:8000/v1"))
    model: str | None = field(default_factory=lambda: os.getenv("CADILLAC_MODEL"))
    context_window: int = field(default_factory=lambda: int(os.getenv("CADILLAC_CONTEXT_WINDOW", "65536")))
    max_context_tokens: int = field(default_factory=lambda: int(os.getenv("CADILLAC_MAX_CONTEXT", "40000")))
    stream: bool = False
    enable_thinking: bool = False
    api_key: str | None = field(default_factory=lambda: os.getenv("CADILLAC_API_KEY"))
    # Max chat completions per second (only applied to chat() / inference calls, not tool dispatch).
    # 0 or negative disables. Env: CADILLAC_RATE_LIMIT.
    rate_limit: float = field(default_factory=lambda: float(os.getenv("CADILLAC_RATE_LIMIT", "0.25")))


class EndpointUnreachable(RuntimeError):
    """Raised by chat() when all retries to the LLM endpoint failed with
    pure connection errors (socket refused, DNS fail, unreachable host).

    Distinct from sentinel-empty-message behavior for transient failures
    (timeouts, HTTP 5xx, chunked-read interruptions) — those indicate the
    endpoint is alive-but-busy and a later round may recover. Connection
    errors mean the endpoint is gone; there's no point burning phase budget
    on additional rounds that will also fail. Top-level run() catches this
    and aborts the build cleanly while still writing phase-history + log
    teardown.
    """


# Per-endpoint last-send timestamp for client-side rate limiting.
# Keyed by api_url so multiple endpoints don't throttle each other.
_RATE_STATE: dict[str, float] = {}


def _pace(api_url: str, rps: float) -> None:
    """Sleep just enough to honor `rps` minimum inter-request interval for this endpoint.

    Zero or negative rps disables pacing. Only called from chat(); tool calls
    (read_file, run_command, etc.) don't hit vLLM so they're not rate-limited.
    """
    if rps is None or rps <= 0:
        return
    min_interval = 1.0 / rps
    now = time.time()
    last = _RATE_STATE.get(api_url, 0.0)
    elapsed = now - last
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _RATE_STATE[api_url] = time.time()


class BuildLogger:
    """Append-only JSON Lines build log for post-mortem analysis."""

    def __init__(self, workspace: str):
        self.path = os.path.join(workspace, ".cadillac", "build.jsonl")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._f = open(self.path, "a")

    def handler(self, event):
        """EventEmitter listener — serialize event to JSONL."""
        entry = {"ts": event.ts, "kind": event.kind, **event.data}
        try:
            self._f.write(json.dumps(entry, default=str) + "\n")
            self._f.flush()
        except (TypeError, ValueError):
            pass  # skip un-serializable events

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


# ── Code fence extraction ─────────────────────────────────────────────────��───

_SRC_EXT_PATTERN = r'\.(?:py|ts|tsx|js|jsx)'


def _extract_code_fences(content: str, lang=None) -> list[tuple[str, str]]:
    """Extract (filename, code) pairs from markdown code fences in model text.

    Matches Python and TS/JS code fences. Deduplicates by filename.
    """
    if not content:
        return []
    seen: set[str] = set()
    results: list[tuple[str, str]] = []
    ext = _SRC_EXT_PATTERN
    fence_langs = r'(?:python|typescript|javascript|ts|js)?'
    patterns = [
        # Pattern 1: ```python filename.py\n...``` (or typescript, etc.)
        (rf'```{fence_langs}\s+(\S+{ext})\s*\n(.*?)```', False),
        # Pattern 2: ```python\n# filename.py\n...```
        (rf'```{fence_langs}\s*\n#\s*(\S+{ext})\s*\n(.*?)```', False),
        # Pattern 3: ```python\n// filename.ts\n...``` (JS/TS comment style)
        (rf'```{fence_langs}\s*\n//\s*(\S+{ext})\s*\n(.*?)```', False),
        # Pattern 4: ```python\n"""filename.py — ..."""\n...```
        (rf'```python\s*\n"""(\S+\.py)\s*[^"]*"""\s*\n(.*?)```', True),
    ]
    for pattern, prepend_docstring in patterns:
        for m in re.finditer(pattern, content, re.DOTALL):
            fname = m.group(1)
            if fname not in seen:
                seen.add(fname)
                code = m.group(2).strip()
                if prepend_docstring:
                    code = '"""' + fname + '"""\n' + code
                results.append((fname, code))
    return results


# ── LLM communication ────────────────────────────────────────────────────────

def _auth_headers(cfg: Config) -> dict:
    """Build auth headers if API key is configured."""
    if cfg.api_key:
        return {"Authorization": f"Bearer {cfg.api_key}"}
    return {}


def detect_model(cfg: Config) -> str:
    resp = requests.get(f"{cfg.api_url}/models", headers=_auth_headers(cfg), timeout=10)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        raise RuntimeError(f"No models available at {cfg.api_url}")
    return data[0]["id"]


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        if msg.get("content"):
            total += estimate_tokens(msg["content"])
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                total += estimate_tokens(tc["function"].get("arguments", ""))
                total += estimate_tokens(tc["function"].get("name", ""))
    return total


def _sanitize_messages_for_send(messages: list[dict]) -> list[dict]:
    """Final defense: ensure every tool_calls[].function.arguments is parseable JSON.

    The vLLM chat template (Qwen, Llama, etc.) JSON-parses tool_call arguments to
    render them in the prompt. Malformed args (e.g. LLM emits bash with unescaped
    quotes like `grep -E "(PASS|FAIL)"`) cause HTTP 400 'Unterminated string'.
    Replace any such malformed args with a stub so the conversation can continue.
    """
    out = []
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            new_tcs = []
            for tc in msg["tool_calls"]:
                args = tc.get("function", {}).get("arguments", "")
                try:
                    parsed = json.loads(args) if args else {}
                    if not isinstance(parsed, (dict, list)):
                        raise ValueError("non-object args")
                    new_tcs.append(tc)
                except (json.JSONDecodeError, ValueError, TypeError):
                    fixed = {**tc, "function": {**tc["function"],
                             "arguments": json.dumps({"_": "malformed args dropped"})}}
                    new_tcs.append(fixed)
            out.append({**msg, "tool_calls": new_tcs})
        else:
            out.append(msg)
    return out


# Token-estimator is char-based (~4 chars/token). BPE tokenizers diverge by
# 1-2% on large contexts — on a 115K input that's ~1K-2K undercount. The
# prior 256-token safety was not enough; we observed one build sending
# input_est=114729 + max_out=16384 = 131113 to a 131112 window (1 over).
# 2048 absorbs ~1.5% undercount at 131K scales. Extract to function so the
# math is unit-testable.
_MAX_OUT_HARD_CAP = 16384       # absolute upper bound for generation budget
_MAX_OUT_FLOOR = 512            # degraded-but-safe minimum (was 2048 — that floor could push past the window)
_CHAT_SAFETY_MARGIN = 2048      # tokens reserved for estimator slop + role overhead


def _compute_max_out(context_window: int, input_est: int) -> int:
    """Derive a safe max_tokens for the outgoing chat call.

    Guarantees `input_est + max_out + SAFETY_MARGIN <= context_window` whenever
    possible. Returns at least `_MAX_OUT_FLOOR` — if even that fits we prefer
    generating something small over a hard crash, and vLLM will reject cleanly
    if the request is still too big.
    """
    available = context_window - input_est - _CHAT_SAFETY_MARGIN
    return max(_MAX_OUT_FLOOR, min(_MAX_OUT_HARD_CAP, available))


def chat(cfg: Config, messages: list[dict], tools: list | None = None, emit=None) -> dict:
    """Send messages to LLM, return assistant message dict."""
    if tools is None:
        tools = TOOL_DEFS
    if emit is None:
        emit = lambda kind, **kw: None
    # Client-side pacing: protect the vLLM server from ourselves. Only applied
    # to chat completions (inference calls), not tool dispatches.
    _pace(cfg.api_url, getattr(cfg, "rate_limit", 0.0))
    messages = _sanitize_messages_for_send(messages)
    input_est = estimate_messages_tokens(messages)
    max_out = _compute_max_out(cfg.context_window, input_est)
    emit("llm_api", input_est=input_est, max_out=max_out)

    body = {
        "model": cfg.model,
        "messages": messages,
        "temperature": 0.6,
        "max_tokens": max_out,
        "stream": cfg.stream,
        "repetition_penalty": 1.05,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if not cfg.enable_thinking and not cfg.api_key:
        # vLLM-specific: disable thinking mode for local endpoints only
        body["chat_template_kwargs"] = {"enable_thinking": False}

    url = f"{cfg.api_url}/chat/completions"
    timeout = 600 if cfg.api_key else 180  # remote APIs need more time (reasoning tokens)

    # Track whether every failure this call saw was a pure connection error.
    # If so, on final attempt we raise EndpointUnreachable instead of returning
    # a sentinel — otherwise a dead endpoint silently burns the whole budget.
    all_conn_errors = True
    last_conn_error: Exception | None = None

    for attempt in range(3):
        try:
            t0 = time.time()
            resp = requests.post(
                url, json=body,
                headers=_auth_headers(cfg),
                stream=cfg.stream, timeout=timeout,
            )
            if cfg.stream:
                return _handle_stream(resp, t0, emit)
            if resp.status_code != 200:
                all_conn_errors = False  # HTTP response received → endpoint alive
                emit("error", msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
                # 400 on attempt 1: try dropping the most recent assistant turn + its tool
                # results — they may contain a poison message the server can't render.
                if resp.status_code == 400 and attempt < 2 and len(messages) > 4:
                    drop_from = len(messages)
                    while drop_from > 2 and messages[drop_from - 1].get("role") in ("tool", "assistant"):
                        drop_from -= 1
                    if drop_from < len(messages):
                        emit("error", msg=f"[recovery] dropping last {len(messages) - drop_from} messages and retrying")
                        body["messages"] = messages[:drop_from]
                if attempt < 2:
                    time.sleep(3)
                    continue
                return {"role": "assistant", "content": ""}
            data = resp.json()
            elapsed = time.time() - t0
            msg = data["choices"][0]["message"]
            usage = data.get("usage", {})
            finish = data["choices"][0].get("finish_reason", "?")
            content = (msg.get("content") or "").strip()
            emit("llm", elapsed=elapsed, tokens=usage.get("completion_tokens", "?"),
                 finish=finish, content=content if content else None)
            return msg
        except requests.exceptions.ConnectionError as e:
            last_conn_error = e
            emit("error", msg=f"API ERROR: {e}")
            if attempt < 2:
                time.sleep(3)
                continue
            # All 3 attempts were pure connection errors — endpoint is objectively
            # down. Raise to the phase loop rather than return an empty sentinel
            # that would just cause the next round to fail identically.
            if all_conn_errors:
                raise EndpointUnreachable(
                    f"cannot reach {cfg.api_url} after 3 attempts: {e}"
                ) from e
            return {"role": "assistant", "content": ""}
        except requests.exceptions.RequestException as e:
            all_conn_errors = False  # timeout / SSL / chunked-read — not a pure connection failure
            emit("error", msg=f"API ERROR: {e}")
            if attempt == 0:
                time.sleep(3)
                continue
            return {"role": "assistant", "content": ""}


def _handle_stream(resp, t0: float, emit) -> dict:
    _MAX_STREAM_LEN = 200_000  # 200KB cap on accumulated content
    content = ""
    tool_calls_map = {}
    first_token = None

    for line in resp.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8")
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            break
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        try:
            delta = data["choices"][0]["delta"]
        except (KeyError, IndexError, TypeError):
            continue
        if first_token is None and (delta.get("content") or delta.get("tool_calls")):
            first_token = time.time()
            emit("llm_stream_ttft", ttft=first_token - t0)
        if delta.get("content"):
            emit("llm_stream_token", token=delta["content"])
            if len(content) < _MAX_STREAM_LEN:
                content += delta["content"]
        if delta.get("tool_calls"):
            for tc in delta["tool_calls"]:
                try:
                    idx = tc.get("index", 0)
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    if tc.get("id"):
                        tool_calls_map[idx]["id"] = tc["id"]
                    if tc.get("function", {}).get("name"):
                        tool_calls_map[idx]["function"]["name"] += tc["function"]["name"]
                    fn_args = tool_calls_map[idx]["function"]["arguments"]
                    if tc.get("function", {}).get("arguments") and len(fn_args) < _MAX_STREAM_LEN:
                        tool_calls_map[idx]["function"]["arguments"] += tc["function"]["arguments"]
                except (KeyError, IndexError, TypeError):
                    continue

    elapsed = time.time() - t0
    emit("llm_stream_done", elapsed=elapsed)
    msg = {"role": "assistant", "content": content or None}
    if tool_calls_map:
        msg["tool_calls"] = [tool_calls_map[i] for i in sorted(tool_calls_map.keys())]
    return msg


# ── Context management ────────────────────────────────────────────────────────

def _dedup_reads(messages: list[dict]) -> list[dict]:
    """Tier 1: Compress earlier read_file results for paths read again later."""
    read_call_info = {}  # tool_call_id -> path
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if tc["function"]["name"] == "read_file":
                    try:
                        args = json.loads(tc["function"]["arguments"])
                        read_call_info[tc["id"]] = args.get("path", "")
                    except (json.JSONDecodeError, KeyError):
                        pass

    path_result_indices: dict[str, list[int]] = {}
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool" and msg.get("tool_call_id") in read_call_info:
            path = read_call_info[msg["tool_call_id"]]
            if path:
                path_result_indices.setdefault(path, []).append(i)

    to_truncate = set()
    for path, indices in path_result_indices.items():
        if len(indices) > 1:
            for idx in indices[:-1]:
                to_truncate.add(idx)

    if not to_truncate:
        return messages

    return [
        {**msg, "content": json.dumps({"_": "[earlier read — see latest]"})}
        if i in to_truncate else msg
        for i, msg in enumerate(messages)
    ]


def _compress_tool_results(messages: list[dict], keep_recent: int = 10) -> list[dict]:
    """Tier 2: Compress tool call args and results in older messages.

    Keeps 500 chars of stderr (errors are most valuable), 200 of stdout/content.
    """
    if len(messages) <= keep_recent + 2:
        return messages

    fixed_start = messages[:2]
    middle = messages[2:-keep_recent]
    recent = messages[-keep_recent:]

    compressed = []
    for msg in middle:
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            try:
                data = json.loads(content)
                if "stderr" in data and isinstance(data["stderr"], str) and len(data["stderr"]) > 500:
                    data["stderr"] = "..." + data["stderr"][-500:]
                if "stdout" in data and isinstance(data["stdout"], str) and len(data["stdout"]) > 200:
                    data["stdout"] = data["stdout"][:200] + "..."
                if "content" in data and isinstance(data["content"], str) and len(data["content"]) > 300:
                    data["content"] = "..." + data["content"][-300:]
                for key in ("files", "matches"):
                    if key in data and isinstance(data[key], list) and len(data[key]) > 10:
                        data[key] = data[key][:10] + [f"... +{len(data[key]) - 10} more"]
                content = json.dumps(data)
            except (json.JSONDecodeError, TypeError):
                if len(content) > 300:
                    content = content[:300] + "... [truncated]"
            compressed.append({**msg, "content": content})
        elif msg.get("role") == "assistant" and msg.get("tool_calls"):
            new_tc = []
            for tc in msg["tool_calls"]:
                fn = tc["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn["arguments"])
                except (json.JSONDecodeError, KeyError):
                    new_tc.append(tc)
                    continue
                if name == "write_file":
                    s = json.dumps({"path": args.get("path", "?"), "_": f"[wrote {len(args.get('content', ''))} chars]"})
                elif name == "edit_file":
                    s = json.dumps({"path": args.get("path", "?"), "_": f"[{len(args.get('edits', []))} edits]"})
                elif name == "read_file":
                    s = json.dumps({"path": args.get("path", "?")})
                elif name == "run_command":
                    s = json.dumps({"command": args.get("command", "?")[:100]})
                else:
                    s = fn["arguments"][:150]
                new_tc.append({**tc, "function": {**fn, "arguments": s}})
            new_msg = {**msg, "tool_calls": new_tc}
            if msg.get("content") and len(msg["content"]) > 150:
                new_msg["content"] = msg["content"][:150] + "..."
            compressed.append(new_msg)
        elif msg.get("role") == "assistant":
            text = msg.get("content", "") or ""
            compressed.append({**msg, "content": text[:200] + "..." if len(text) > 200 else text})
        else:
            compressed.append(msg)

    return fixed_start + compressed + recent


def _summarize_middle(messages: list[dict], keep_recent: int = 10) -> list[dict]:
    """Tier 3: Replace middle messages with a compact text summary."""
    if len(messages) <= keep_recent + 2:
        return messages

    recent_start = len(messages) - keep_recent
    # Don't orphan tool results — pull split point back to an assistant message
    while recent_start > 2 and messages[recent_start].get("role") == "tool":
        recent_start -= 1

    fixed_start = messages[:2]
    middle = messages[2:recent_start]
    recent = messages[recent_start:]

    summary_parts = []
    for msg in middle:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            calls = []
            for tc in msg["tool_calls"]:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, KeyError):
                    args = {}
                if name == "write_file":
                    calls.append(f"write_file({args.get('path', '?')})")
                elif name == "edit_file":
                    calls.append(f"edit_file({args.get('path', '?')})")
                elif name == "run_command":
                    calls.append(f"run({args.get('command', '?')[:60]})")
                elif name == "read_file":
                    calls.append(f"read({args.get('path', '?')})")
                else:
                    calls.append(f"{name}()")
            summary_parts.append(", ".join(calls))
        elif msg.get("role") == "tool":
            content = msg.get("content", "")
            try:
                data = json.loads(content)
                ec = data.get("exit_code")
                if ec is not None:
                    stderr = data.get("stderr", "")
                    if ec != 0 and stderr:
                        summary_parts.append(f"  -> exit {ec}: {stderr.strip()[:80]}")
                    else:
                        summary_parts.append(f"  -> exit {ec}")
                elif "error" in data:
                    summary_parts.append(f"  -> error: {str(data['error'])[:80]}")
            except (json.JSONDecodeError, TypeError):
                pass

    if not summary_parts:
        return fixed_start + recent

    summary_text = "[Earlier actions summary]\n" + "\n".join(summary_parts[-30:])
    return fixed_start + [{"role": "user", "content": summary_text}] + recent


def trim_context(messages: list[dict], max_tokens: int = 40000, keep_recent: int = 10) -> list[dict]:
    """Progressive context compression with three tiers.

    Tier 1 (dedup): Compress duplicate read_file results, keeping latest.
    Tier 2 (compress): Shrink tool args/results — keep 500 chars stderr, 200 stdout.
    Tier 3 (summarize): Replace middle messages with a compact action summary.
    Fallback: Drop oldest message groups until under budget.
    """
    if estimate_messages_tokens(messages) <= max_tokens:
        return messages

    # Tier 1: deduplicate read_file results
    messages = _dedup_reads(messages)
    if estimate_messages_tokens(messages) <= max_tokens:
        return messages

    # Tier 2: compress tool results and call arguments
    messages = _compress_tool_results(messages, keep_recent)
    if estimate_messages_tokens(messages) <= max_tokens:
        return messages

    # Tier 3: summarize middle messages into text
    messages = _summarize_middle(messages, keep_recent)
    if estimate_messages_tokens(messages) <= max_tokens:
        return messages

    # Fallback: drop oldest messages until under budget
    total = estimate_messages_tokens(messages)
    while total > max_tokens and len(messages) > 2 + keep_recent:
        removed = messages.pop(2)
        total -= estimate_tokens(removed.get("content", ""))
        while len(messages) > 2 and messages[2].get("role") == "tool":
            removed = messages.pop(2)
            total -= estimate_tokens(removed.get("content", ""))

    return messages


def _create_phase_summary(phase_name: str, manifest: FileManifest, results=None, build_log=None) -> str:
    """Summarize a completed phase for injection into the next phase's context."""
    parts = [f"[Previous {phase_name} phase summary]"]
    parts.append(manifest.to_status())
    if results:
        for r in results:
            status = "PASS" if r.passed else f"FAIL: {r.output[:200]}"
            parts.append(f"  {r.name}: {status}")
    if build_log:
        parts.append("Recent log: " + " | ".join(build_log[-5:]))
    return "\n".join(parts)


# ── JSON extraction ───────────────────────────────────────────────────────────

def extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    depth = 0
    start = None
    for i, c in enumerate(text):
        if c == '{':
            if depth == 0:
                start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    fixed = re.sub(r',\s*([}\]])', r'\1', candidate)
                    try:
                        return json.loads(fixed)
                    except json.JSONDecodeError:
                        pass
                start = None
    return None


# ── Tool execution helpers ────────────────────────────────────────────────────

def _display_call_summary(fn_name: str, fn_args: dict) -> str:
    if fn_name == "write_file":
        lines = fn_args.get("content", "").count("\n") + 1
        return f"{fn_args.get('path', '?')}) [{lines} lines]"
    elif fn_name == "edit_file":
        return f"{fn_args.get('path', '?')}) [{len(fn_args.get('edits', []))} edits]"
    elif fn_name == "line_edit":
        return f"{fn_args.get('path', '?')}, {fn_args.get('start_line', '?')}-{fn_args.get('end_line', '?')})"
    elif fn_name == "run_command":
        return f"{fn_args.get('command', '?')[:100]}"
    else:
        s = json.dumps(fn_args)
        return f"{s[:120]}{'...' if len(s) > 120 else ''}"


def _result_summary(result_str: str) -> str:
    return result_str[:300] + (f"... ({len(result_str)} bytes)" if len(result_str) > 300 else "")


def process_tool_calls(
    msg: dict, messages: list[dict], executor: ToolExecutor,
    error_tracker: ErrorTracker, batch_tracker: BatchTracker | None,
    progress: Progress, emit=None, allowed_tools: set[str] | None = None,
) -> tuple[int, str | None, str | None, tuple[str, dict] | None]:
    """Process tool calls. Returns (n_executed, batch_nudge, error_intervention, last_run_cmd).
    last_run_cmd is (command_string, result_dict) for the last run_command call, or None.
    allowed_tools: if set, reject tool calls not in this set (vLLM doesn't enforce tool schemas)."""
    if emit is None:
        emit = lambda kind, **kw: None
    if not msg.get("tool_calls"):
        return 0, None, None, None

    # Sanitize tool calls
    sanitized = []
    parsed = []
    malformed_results = []
    for tc in msg["tool_calls"]:
        fn_name = tc["function"]["name"]
        # Reject tools not in the allowed set (vLLM can hallucinate tool calls)
        if allowed_tools and fn_name not in allowed_tools:
            fixed = {**tc, "function": {**tc["function"], "arguments": json.dumps({"error": "not available"})}}
            sanitized.append(fixed)
            malformed_results.append({
                "role": "tool", "tool_call_id": tc.get("id", "err"),
                "content": json.dumps({"error": f"Tool '{fn_name}' is not available. Use edit_file, write_file, line_edit, or run_command."}),
            })
            continue
        try:
            fn_args = json.loads(tc["function"]["arguments"])
            if not isinstance(fn_args, dict):
                fn_args = {}  # LLM sent a non-object JSON value; ignore args
            sanitized.append(tc)
            parsed.append((tc, fn_name, fn_args))
        except json.JSONDecodeError:
            fixed = {**tc, "function": {**tc["function"], "arguments": json.dumps({"error": "truncated"})}}
            sanitized.append(fixed)
            malformed_results.append({
                "role": "tool", "tool_call_id": tc.get("id", "err"),
                "content": json.dumps({"error": "Output truncated. Try again."}),
            })

    msg["tool_calls"] = sanitized
    messages.append(msg)
    for mr in malformed_results:
        messages.append(mr)

    if not parsed:
        return 0, None, None, None

    # Execute tools
    batch_nudge = None
    error_intervention = None
    last_run_cmd = None
    parallel_batch = []

    def flush_batch(batch):
        nonlocal batch_nudge
        if not batch:
            return
        if len(batch) == 1:
            tc, fn_name, fn_args = batch[0]
            summary = _display_call_summary(fn_name, fn_args)
            emit("tool_call", name=fn_name, summary=summary)
            result = executor.dispatch(fn_name, fn_args)
            result_str = json.dumps(result)
            emit("tool_result", name=fn_name, summary=_result_summary(result_str))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_str})
        else:
            emit("tool_parallel", count=len(batch))
            for tc, fn_name, fn_args in batch:
                summary = _display_call_summary(fn_name, fn_args)
                emit("tool_call", name=fn_name, summary=summary)
            with ThreadPoolExecutor(max_workers=min(len(batch), 4)) as pool:
                futures = {}
                for idx, (tc, fn_name, fn_args) in enumerate(batch):
                    futures[pool.submit(executor.dispatch, fn_name, fn_args)] = idx
                indexed = [None] * len(batch)
                for future in as_completed(futures):
                    try:
                        indexed[futures[future]] = future.result()
                    except Exception as e:
                        indexed[futures[future]] = {"error": f"Parallel execution failed: {e}"}
            for i, (tc, fn_name, fn_args) in enumerate(batch):
                result_str = json.dumps(indexed[i])
                emit("tool_result", name=fn_name, summary=_result_summary(result_str))
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_str})

        # Check batch tracker for writes
        for _, bn, ba in batch:
            if bn in ("write_file", "write_test") and batch_tracker:
                nudge = batch_tracker.file_written(ba.get("path", ""))
                if nudge:
                    batch_nudge = nudge
            if bn in ("write_file", "write_test"):
                progress.mark_file_done(ba.get("path", ""))
                emit("file_written", path=ba.get("path", ""))

    for tc, fn_name, fn_args in parsed:
        if fn_name in ToolExecutor.PARALLEL_SAFE:
            parallel_batch.append((tc, fn_name, fn_args))
        else:
            flush_batch(parallel_batch)
            parallel_batch = []
            # Sequential execution
            summary = _display_call_summary(fn_name, fn_args)
            emit("tool_call", name=fn_name, summary=summary)
            result = executor.dispatch(fn_name, fn_args)
            result_str = json.dumps(result)
            emit("tool_result", name=fn_name, summary=_result_summary(result_str))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_str})

            # Track run_command results
            if fn_name == "run_command" and isinstance(result, dict):
                cmd = fn_args.get("command", "")
                last_run_cmd = (cmd, result)
                err_text = extract_failure_text(result, cmd)
                if err_text:
                    # Keep ErrorTracker in sync with current executor phase/round/scratch
                    # so auto-escalation can write tried_failed entries with the right
                    # phase context — works for run(), iterate(), and modular pipelines.
                    error_tracker.scratch = getattr(executor, "scratch", None)
                    error_tracker.phase_name = getattr(executor, "phase_name", "?")
                    error_tracker.round_num = getattr(executor, "round_num", 0)
                    error_tracker.last_command = cmd
                    intervention = error_tracker.record(err_text)
                    if intervention:
                        error_intervention = intervention

    flush_batch(parallel_batch)
    return len(parsed), batch_nudge, error_intervention, last_run_cmd


# ── Phase-specific message builders ───────────────────────────────────────────

def _build_messages(system_prompt: str, task: str, extra: list[dict] | None = None,
                    scratch_text: str = "") -> list[dict]:
    if scratch_text and scratch_text.strip():
        system_prompt = system_prompt + "\n\n## Your Scratchpad (notes you wrote earlier this build)\n" + scratch_text.strip()
    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    if extra:
        msgs.extend(extra)
    return msgs


def _refresh_scratch_in_messages(messages: list[dict], system_prompt_base: str, scratch_text: str) -> None:
    """Rewrite the system message in-place with a fresh scratch section appended."""
    if not messages:
        return
    if scratch_text and scratch_text.strip():
        new_content = system_prompt_base + "\n\n## Your Scratchpad (notes you wrote earlier this build)\n" + scratch_text.strip()
    else:
        new_content = system_prompt_base
    messages[0] = {"role": "system", "content": new_content}


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\\u001b\[[0-9;]*[A-Za-z]")


def _error_fingerprint(text: str) -> str:
    """Normalized error signature — used for smart phase exit.
    Strips ANSI escapes, collapses whitespace, drops file paths/line numbers,
    then takes a wider 200-char slice so test-failure assertions land in it.
    """
    if not text:
        return ""
    s = _ANSI_RE.sub("", text)
    s = re.sub(r"/[\w./-]+:\d+", "", s)  # drop file:line refs that vary by attempt
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200]


def _is_phase_stuck(fingerprints: list[str], threshold: int = 3, lookback: int = 5,
                    overlap: float = 0.8, min_len: int = 80) -> bool:
    """True if `threshold` of last `lookback` errors are >`overlap` word-similar."""
    recent = [f for f in fingerprints[-lookback:] if len(f) >= min_len]
    if len(recent) < threshold:
        return False
    for i, ref in enumerate(recent):
        ref_words = set(ref.lower().split())
        if not ref_words:
            continue
        matches = 1
        for j, other in enumerate(recent):
            if i == j:
                continue
            other_words = set(other.lower().split())
            if not other_words:
                continue
            jacc = len(ref_words & other_words) / len(ref_words | other_words)
            if jacc > overlap:
                matches += 1
        if matches >= threshold:
            return True
    return False


# ── Git integration (lightweight, for rollback) ──────────────────────────────

def _git_init(workspace: str):
    """Initialize a git repo in the workspace with an initial commit."""
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=workspace, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=workspace, capture_output=True)


def _git_checkpoint(workspace: str, phase_name: str):
    """Commit current state as a phase checkpoint."""
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=workspace, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", f"checkpoint: {phase_name}", "--allow-empty"],
                   cwd=workspace, capture_output=True)


def _git_rollback(workspace: str, n: int = 1):
    """Rollback to n commits before HEAD."""
    import subprocess
    subprocess.run(["git", "reset", "--hard", f"HEAD~{n}"], cwd=workspace, capture_output=True)


# ── Checkpointing ────────────────────────────────────────────────────────────

def _save_checkpoint(workspace: str, state: PhaseState, manifest: FileManifest,
                     plan: dict | None, architecture_text: str, entry_point: str, task: str):
    """Save build state for resume."""
    checkpoint = {
        "phase": state.current.value,
        "round_in_phase": state.round_in_phase,
        "total_rounds": state.total_rounds,
        "validate_retries": state.validate_retries,
        "manifest_files": list(manifest.files.keys()),
        "entry_point": entry_point,
        "task": task,
        "architecture": architecture_text,
        "ts": time.time(),
    }
    ckpt_dir = os.path.join(workspace, ".cadillac")
    os.makedirs(ckpt_dir, exist_ok=True)
    with open(os.path.join(ckpt_dir, "checkpoint.json"), "w") as f:
        json.dump(checkpoint, f, indent=2)


def _load_checkpoint(workspace: str) -> dict | None:
    """Load checkpoint if it exists."""
    path = os.path.join(workspace, ".cadillac", "checkpoint.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


# ── Workspace loader (for iterate/debug) ─────────────────────────────────────

def _load_workspace(workspace: str, lang=None) -> tuple[dict | None, FileManifest]:
    """Load plan.json and rebuild manifest from existing files."""
    plan = None
    plan_path = os.path.join(workspace, "plan.json")
    if os.path.exists(plan_path):
        with open(plan_path) as f:
            plan = json.load(f)

    exts = tuple(lang.extensions) if lang else (".py",)
    skip_dirs = {"__pycache__", ".venv", ".cadillac"}
    if lang and lang.family == "node":
        skip_dirs.add("node_modules")

    manifest = FileManifest()
    for root, _, files in os.walk(workspace):
        if any(sd in root for sd in skip_dirs):
            continue
        for fname in files:
            if not fname.endswith(exts):
                continue
            path = os.path.relpath(os.path.join(root, fname), workspace)
            full = os.path.join(root, fname)
            with open(full) as f:
                content = f.read()
            manifest.record(path, content)

    return plan, manifest


# ── Modular detection ─────────────────────────────────────────────────────────

def _parse_critic_findings(text: str) -> list[dict]:
    """Try to extract a JSON array of critic findings from LLM response."""
    # Try to find JSON array in the response
    text = text.strip()
    # Remove markdown fences if present
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("["):
                text = part
                break
    # Try parsing
    if "[" in text:
        start = text.index("[")
        # Find matching ]
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        findings = json.loads(text[start:i+1])
                        if isinstance(findings, list):
                            return [f for f in findings if isinstance(f, dict) and "issue" in f]
                    except json.JSONDecodeError:
                        pass
                    break
    return []


def _format_critic_findings(findings: list[dict]) -> str:
    """Format structured critic findings as numbered text for BUILD injection."""
    lines = ["CRITIC FINDINGS (fix these FIRST, in order):"]
    for i, f in enumerate(findings, 1):
        sev = f.get("severity", "medium").upper()
        file = f.get("file", "?")
        line = f.get("line", "?")
        issue = f.get("issue", "unknown")
        fix = f.get("fix", "")
        entry = f"  {i}. [{sev}] {file} ~line {line}: {issue}"
        if fix:
            entry += f"\n     Fix: {fix}"
        lines.append(entry)
    return "\n".join(lines)


def _should_use_modular(architecture_text: str) -> bool:
    """Decide flat vs modular pipeline from the architecture text.

    Modular pipeline kicks in for any of these strong signals:
      1. >=15 source-file references (Python, TS/JS, Vue, Svelte, Go, Rust)
      2. Explicit "## Modules" heading in the architecture
      3. Full-stack split: both `backend/` AND `frontend/` (or `client/server`,
         `api/web`) referenced — natural module boundary
      4. >=4 distinct top-level subdirectory mentions enumerated as
         components (e.g. `auth/, listings/, bids/, orders/`) — that's a
         module list whether the LLM labeled it as such or not

    Old heuristic counted only `.py` refs, so a 25-file full-stack project
    with mostly `.tsx` files (eBay-style) would silently fall back to flat
    despite obvious module boundaries.
    """
    text = architecture_text
    text_lower = text.lower()

    # 1. Count source files across every common language
    file_refs = len(re.findall(
        r"\w+\.(?:py|ts|tsx|js|jsx|vue|svelte|go|rs|java|kt|cs)\b",
        text,
    ))
    if file_refs >= 15:
        return True

    # 2. Explicit modules section
    if "## modules" in text_lower:
        return True

    # 3. Full-stack split — strong signal regardless of file count
    backend_signals = ("backend/", "server/", "api/")
    frontend_signals = ("frontend/", "client/", "web/", "ui/")
    has_backend = any(s in text_lower for s in backend_signals)
    has_frontend = any(s in text_lower for s in frontend_signals)
    if has_backend and has_frontend:
        return True

    # 4. >=4 distinct subdirectory components enumerated. Looks for the
    # classic comma/slash-separated list pattern: `auth/, listings/, bids/`
    # or `auth/, listings/, bids/, orders/, reviews/`.
    subdir_matches = re.findall(r"\b([a-z][a-z_]{1,15})/", text_lower)
    distinct_subdirs = {
        s for s in subdir_matches
        # Exclude common path-suffix words that aren't module names
        if s not in {
            "src", "tests", "test", "lib", "bin", "etc", "var", "usr",
            "node_modules", "dist", "build", "public", "static", "assets",
            "docs", "examples", "scripts",
        }
    }
    if len(distinct_subdirs) >= 4:
        return True

    return False


def _split_markdown_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown text into (header, body) pairs by ## headers."""
    sections: list[tuple[str, str]] = []
    current_header = ""
    current_body: list[str] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            if current_header or current_body:
                sections.append((current_header, "\n".join(current_body)))
            current_header = line.lstrip("# ").strip()
            current_body = []
        else:
            current_body.append(line)
    if current_header or current_body:
        sections.append((current_header, "\n".join(current_body)))
    return sections


def _extract_project_context(architecture_text: str, modular_plan) -> str:
    """Extract compact project-wide context from architecture text.

    Returns technology decisions, constraints, and module overview that every
    module needs to follow — regardless of whether it appears in the module's
    own architecture excerpt.  ~200-400 tokens.
    """
    lines: list[str] = []

    # Declared pip dependencies = the technology choices
    if modular_plan.dependencies:
        lines.append(f"REQUIRED PACKAGES: {', '.join(modular_plan.dependencies)}")
        lines.append("Use ONLY these packages (plus stdlib). Do NOT introduce alternatives.")

    # Pull relevant top-level sections from the architecture doc
    keep_headers = {"architecture", "requirements", "risks"}
    for header, body in _split_markdown_sections(architecture_text):
        if header.lower() in keep_headers:
            lines.append(f"\n{header.upper()}:\n{body.strip()}")

    if modular_plan.entry_point:
        lines.append(f"\nENTRY POINT: {modular_plan.entry_point}")

    # All module names so each module sees the whole system
    if modular_plan.modules:
        lines.append("\nALL MODULES:")
        for mod in modular_plan.modules:
            lines.append(f"  - {mod.name}: {mod.purpose}")

    return "\n".join(lines)


# ── Per-module build ─────────────────────────────────────────────────────────

def _build_module(
    module_name: str,
    modular_plan: ModularPlan,
    workspace: str,
    cfg: Config,
    manifest: FileManifest,
    emit,
    lessons_text: str = "",
    architecture_text: str = "",
    lang=None,
) -> tuple[bool, list[str], dict[str, int]]:
    """Scaffold and build a single module.

    Returns (success, error_messages, rounds_used) where rounds_used is
    `{"scaffold": N, "build": M}`. Callers aggregate these into
    `state.phase_rounds_used` so modular builds' real work shows up in the
    cross-build phase-history (otherwise top-level SCAFFOLD/BUILD rounds read
    ~1 each, making #4 memory-aware budgets useless on modular projects).
    """
    from .codemap import ModuleCodeMapBuilder

    rounds_used = {"scaffold": 0, "build": 0}
    mod = modular_plan.get_module(module_name)
    if not mod:
        return False, [f"Module '{module_name}' not found in plan"], rounds_used

    module_path = mod.path.rstrip("/")
    os.makedirs(os.path.join(workspace, module_path), exist_ok=True)

    # Build scoped context
    interface_stubs = modular_plan.get_dependency_interfaces(module_name)

    # Architecture excerpt: just this module's section
    arch_excerpt = f"Module: {mod.name}\nPurpose: {mod.purpose}\n"
    arch_excerpt += f"Exports: {', '.join(mod.exports)}\n"
    arch_excerpt += f"Interfaces:\n"
    for iface in mod.interfaces:
        arch_excerpt += f"  - {iface}\n"
    arch_excerpt += f"Files:\n"
    for f in mod.files:
        fpath = f.get("path", "") if isinstance(f, dict) else f
        purpose = f.get("purpose", "") if isinstance(f, dict) else ""
        arch_excerpt += f"  - {fpath}: {purpose}\n"

    # Project-wide context (tech stack, constraints, all module names)
    project_context = _extract_project_context(architecture_text, modular_plan)

    # Create scoped executor and code map builder
    executor = ModuleScopedExecutor(workspace, manifest, module_path,
                                     context_budget=cfg.max_context_tokens)
    executor.build_mode = False
    executor.scratch = Scratch(workspace, module=mod.path.rstrip("/"))
    executor.phase_name = f"MODULE/{module_name}/SCAFFOLD"
    executor.round_num = 0
    error_tracker = ErrorTracker(manifest)
    batch_tracker = BatchTracker(
        {"files": mod.files, "build_order": mod.build_order},
        run_cmd=lang.run_cmd if lang else "python3",
        entry_point=mod.test_file or "main.py",
    )
    progress = Progress(task=f"Module: {module_name}", workspace=workspace)

    # Context-proportional scratch caps — big models get richer scratch visibility.
    _sbudget = _scratch_budget(cfg.max_context_tokens, len(modular_plan.modules))
    # Read dependency scratches (cross-wave memory from upstream modules)
    dep_scratches = Scratch.read_dependencies(workspace, mod.depends_on, cap_per=_sbudget["dep"]) if mod.depends_on else ""
    # Also include workspace-root scratch so per-module builds see task constraints
    # (reminder entries pinned there after PLAN parse).
    root_scratch = Scratch(workspace).read(max_chars=_sbudget["root"])

    emit("log", msg=f"[MODULE {module_name}] Scaffolding {len(mod.files)} files...")
    emit("module_start", module=module_name, n_files=len(mod.files))

    # ── Scaffold loop ──
    scaffold_prompt = build_module_scaffold_prompt(
        module_name=module_name,
        module_purpose=mod.purpose,
        module_path=module_path + "/",
        interface_stubs=interface_stubs,
        architecture_excerpt=arch_excerpt,
        project_context=project_context,
        manifest_summary="",
        progress_context="",
        lessons_text=lessons_text,
        lang=lang,
    )
    # Inject root + dependency + own scratch into system prompt (root has task constraints)
    own_scratch = executor.scratch.read()
    combined_scratch = "\n\n".join(p for p in [root_scratch, dep_scratches, own_scratch] if p.strip())
    messages = _build_messages(scaffold_prompt, f"Build the {module_name} module.",
                               scratch_text=combined_scratch)

    directive = batch_tracker.get_current_directive()
    if directive:
        messages.append({"role": "user", "content": directive})

    # Adaptive scaffold budget: scales with file count
    scaffold_budget = max(15, len(mod.files) + 5)
    scaffold_fingerprints: list[str] = []
    no_tool_rounds = 0
    for _scaffold_round in range(scaffold_budget):
        executor.round_num = _scaffold_round + 1
        messages = trim_context(messages, max_tokens=cfg.max_context_tokens)
        # Refresh scratch every 3 rounds (root has constraints + dep + own)
        if _scaffold_round > 0 and _scaffold_round % 3 == 0:
            _refresh_scratch_in_messages(messages, scaffold_prompt,
                                         "\n\n".join(p for p in [root_scratch, dep_scratches, executor.scratch.read()] if p.strip()))
        msg = chat(cfg, messages, emit=emit)

        if not msg.get("tool_calls"):
            content = msg.get("content", "")
            messages.append(msg)
            # Try code fence extraction
            extracted = _extract_code_fences(content)
            if extracted:
                for path, code in extracted:
                    result = executor.write_file(path, code)
                    if result.get("status") == "ok":
                        emit("log", msg=f"[MODULE {module_name}] Extracted {path}")
                no_tool_rounds = 0
                messages.append({"role": "user", "content":
                    "I extracted the code. Next file — use write_file() as a tool call."})
                continue

            no_tool_rounds += 1
            if no_tool_rounds >= 3 or batch_tracker.all_done:
                break
            nudge = batch_tracker.get_remaining_nudge()
            if nudge:
                messages.append({"role": "user", "content": nudge})
            continue

        no_tool_rounds = 0
        n_exec, batch_nudge, error_intervention, _ = process_tool_calls(
            msg, messages, executor, error_tracker, batch_tracker, progress, emit=emit,
        )

        if error_intervention:
            messages.append({"role": "user", "content": error_intervention})
        elif batch_nudge:
            emit("log", msg=f"[MODULE {module_name}] {batch_nudge[:60]}...")
            messages.append({"role": "user", "content": batch_nudge})

        if batch_tracker.all_done:
            break

    rounds_used["scaffold"] = executor.round_num
    emit("log", msg=f"[MODULE {module_name}] Scaffold done, {len([f for f in manifest.files if f.startswith(module_path)])} files")

    # ── Post-scaffold: strip .js extensions from TS imports ──
    if lang and lang.family == "node":
        n = _strip_js_extensions_from_ts(workspace, manifest)
        if n:
            emit("log", msg=f"[MODULE {module_name}] Stripped .js extensions from {n} TS files")

    # ── Post-scaffold validation (early detection) ──
    # Detect conflicts early so the build loop can fix them via LLM reasoning
    from .validate import check_framework_conflicts, check_syntax
    pre_build_checks: list = []
    pre_build_checks.extend(check_syntax(workspace, lang=lang))
    pre_build_checks.extend(check_framework_conflicts(
        workspace, expected_packages=modular_plan.dependencies, lang=lang,
    ))
    pre_build_failures = "\n".join(
        f"[{r.name}] {r.output}" for r in pre_build_checks if not r.passed
    )
    if pre_build_failures:
        emit("log", msg=f"[MODULE {module_name}] Post-scaffold issues detected, will fix in build loop")

    # ── Build loop (fix errors, run module tests) ──
    code_map_builder = ModuleCodeMapBuilder(workspace, module_path, budget_tokens=20000, lang=lang)
    code_map = code_map_builder.build()

    executor.build_mode = True
    executor.config_writes_allowed = False
    executor.phase_name = f"MODULE/{module_name}/BUILD"
    executor.grant_rewrites(max(len(mod.files), 5))

    build_prompt = build_module_build_prompt(
        module_name=module_name,
        module_test=mod.test_file or "",
        interface_stubs=interface_stubs,
        code_map=code_map,
        validation_failures=pre_build_failures,
        lessons_text=lessons_text,
        lang=lang,
    )
    combined_scratch = "\n\n".join(p for p in [root_scratch, dep_scratches, executor.scratch.read()] if p.strip())
    messages = _build_messages(build_prompt, f"Fix and test the {module_name} module.",
                               scratch_text=combined_scratch)

    if mod.test_file:
        test_cmd = " ".join(lang.test_cmd) + " " + mod.test_file
    else:
        test_cmd = ""
    if test_cmd:
        messages.append({"role": "user", "content": f"Run module tests: {test_cmd}"})

    # Adaptive build budget: scales with file count
    build_budget = max(20, len(mod.files) * 4)
    build_fingerprints: list[str] = []
    no_tool_rounds = 0
    for _build_round in range(build_budget):
        executor.round_num = _build_round + 1
        messages = trim_context(messages, max_tokens=cfg.max_context_tokens)
        # Refresh scratch every 3 rounds (root + deps + own)
        if _build_round > 0 and _build_round % 3 == 0:
            _refresh_scratch_in_messages(messages, build_prompt,
                                         "\n\n".join(p for p in [root_scratch, dep_scratches, executor.scratch.read()] if p.strip()))
        msg = chat(cfg, messages, emit=emit)

        if not msg.get("tool_calls"):
            messages.append(msg)
            no_tool_rounds += 1
            if no_tool_rounds >= 3:
                break
            if test_cmd:
                messages.append({"role": "user", "content": f"Run {test_cmd} to test."})
            continue

        no_tool_rounds = 0
        n_exec, _, error_intervention, last_run = process_tool_calls(
            msg, messages, executor, error_tracker, None, progress, emit=emit,
        )

        # Per-round hygiene for the module BUILD loop too.
        _post_round_safeguards(workspace, lang, manifest, messages, msg, emit)

        if error_intervention:
            messages.append({"role": "user", "content": error_intervention})

        # Track error fingerprints + smart phase exit
        if last_run:
            _, _cmd_result = last_run
            _stderr = (_cmd_result.get("stderr") or "").strip()
            _stdout = (_cmd_result.get("stdout") or "").strip()
            _err_text = _stderr if _stderr else _stdout
            if _cmd_result.get("exit_code", 0) != 0 and _err_text:
                fp = _error_fingerprint(_err_text)
                if fp:
                    build_fingerprints.append(fp)

        if _is_phase_stuck(build_fingerprints):
            emit("log", msg=f"[MODULE {module_name}] PHASE_STUCK — exiting build loop early")
            break

        # Check if tests pass
        if last_run:
            cmd_str, cmd_result = last_run
            if ("pytest" in cmd_str and cmd_result.get("exit_code") == 0):
                emit("log", msg=f"[MODULE {module_name}] Tests pass!")
                break
            if (cmd_result.get("exit_code") == 0
                    and "Traceback" not in cmd_result.get("stderr", "")):
                emit("log", msg=f"[MODULE {module_name}] Command succeeded")

        if error_tracker.is_stuck():
            emit("log", msg=f"[MODULE {module_name}] Stuck, stopping build")
            break

    # Build-phase rounds: executor.round_num reflects the last BUILD round,
    # but if the scaffold succeeded and no build rounds ran (e.g. scaffold
    # alone was enough), the build value may equal scaffold's final value.
    # Using max() against scaffold gives a non-negative delta.
    rounds_used["build"] = max(0, executor.round_num - rounds_used["scaffold"])

    # ── Module validation ──
    results = run_module_validation(workspace, module_path, mod.test_file, lang=lang)
    errors = [r for r in results if not r.passed and r.severity == "error"]
    if errors:
        error_msgs = [f"[{r.name}] {r.output[:200]}" for r in errors]
        emit("log", msg=f"[MODULE {module_name}] Validation: {len(errors)} error(s)")
        # Persist module failure context into module scratch for the next wave
        try:
            executor.scratch.append(
                "tried_failed",
                f"Module {module_name} failed validation: " + "; ".join(error_msgs[:2])[:180],
                phase=f"MODULE/{module_name}/VALIDATE",
                round_num=0,
            )
        except Exception:
            pass
        emit("module_complete", module=module_name, success=False)
        return False, error_msgs, rounds_used

    # ── Module success: extract real interfaces + per-module checkpoint ──
    try:
        real_ifaces = extract_real_interfaces(workspace, mod)
        if real_ifaces:
            mod.real_interfaces = real_ifaces
            emit("log", msg=f"[MODULE {module_name}] Captured {len(real_ifaces)} real interface file(s)")
    except Exception as e:
        emit("log", msg=f"[MODULE {module_name}] Interface extraction failed: {e}")

    try:
        ckpt_dir = os.path.join(workspace, ".cadillac", "modules")
        os.makedirs(ckpt_dir, exist_ok=True)
        mod_files = [f for f in manifest.files if f.startswith(module_path)]
        ckpt = {
            "module": module_name,
            "path": mod.path,
            "files": mod_files,
            "real_interfaces": mod.real_interfaces,
            "scratch": executor.scratch.read(max_chars=_sbudget["entry"]),
            "success": True,
            "ts": time.time(),
        }
        with open(os.path.join(ckpt_dir, f"{module_name}.checkpoint.json"), "w") as f:
            json.dump(ckpt, f, indent=2)
    except Exception as e:
        emit("log", msg=f"[MODULE {module_name}] Checkpoint save failed: {e}")

    emit("log", msg=f"[MODULE {module_name}] Build complete!")
    emit("module_complete", module=module_name, success=True)
    return True, [], rounds_used


def _build_module_wave(
    wave: list[str],
    modular_plan: ModularPlan,
    workspace: str,
    cfg: Config,
    manifest: FileManifest,
    emit,
    lessons_text: str = "",
    architecture_text: str = "",
    max_parallel: int = 3,
    parallel: bool = False,
    lang=None,
) -> tuple[dict[str, bool], dict[str, int]]:
    """Build a wave of independent modules.

    Returns `(results, wave_rounds)` where `results = {module_name: success}`
    and `wave_rounds = {"scaffold": sum_scaffold, "build": sum_build}`
    aggregated across all modules in this wave. The caller sums across waves
    into state.phase_rounds_used so modular builds' real work shows up in
    cross-build phase history.
    """
    wave_rounds = {"scaffold": 0, "build": 0}
    if len(wave) <= 1 or not parallel:
        # Sequential
        results = {}
        for mod_name in wave:
            ok, errs, rounds = _build_module(
                mod_name, modular_plan, workspace, cfg,
                manifest, emit, lessons_text, architecture_text, lang=lang,
            )
            results[mod_name] = ok
            for k in wave_rounds:
                wave_rounds[k] += rounds.get(k, 0)
        return results, wave_rounds

    # Parallel for multiple independent modules
    emit("log", msg=f"[WAVE] Building {len(wave)} modules in parallel: {', '.join(wave)}")
    results = {}

    def _build_one(mod_name):
        ok, errs, rounds = _build_module(
            mod_name, modular_plan, workspace, cfg,
            manifest, emit, lessons_text, architecture_text, lang=lang,
        )
        return mod_name, ok, errs, rounds

    with ThreadPoolExecutor(max_workers=min(len(wave), max_parallel)) as pool:
        futures = {pool.submit(_build_one, name): name for name in wave}
        for future in as_completed(futures):
            try:
                mod_name, ok, errs, rounds = future.result()
            except Exception as e:
                mod_name = futures[future]
                ok, errs, rounds = False, [f"Exception: {e}"], {"scaffold": 0, "build": 0}
            results[mod_name] = ok
            for k in wave_rounds:
                wave_rounds[k] += rounds.get(k, 0)
            if not ok:
                emit("log", msg=f"[WAVE] {mod_name} failed: {'; '.join(errs[:2])}")

    return results, wave_rounds


def _build_module_summaries(modular_plan: ModularPlan, manifest: FileManifest) -> str:
    """Build compact module summaries for the integration prompt."""
    parts = []
    for mod in modular_plan.modules:
        mod_files = [f for f in manifest.files if f.startswith(mod.path.rstrip("/") + "/")]
        parts.append(f"### {mod.name} ({mod.path})")
        parts.append(f"Purpose: {mod.purpose}")
        parts.append(f"Exports: {', '.join(mod.exports)}")
        parts.append(f"Interfaces:")
        for iface in mod.interfaces:
            parts.append(f"  - {iface}")
        parts.append(f"Files: {', '.join(mod_files)}")
        # Include __init__.py exports if available
        init_path = f"{mod.path.rstrip('/')}/__init__.py"
        summary = manifest.get_summary(init_path)
        if summary:
            parts.append(f"__init__.py:\n{summary}")
        parts.append("")
    return "\n".join(parts)


def _iterate_module(
    module_name: str,
    modular_plan: ModularPlan,
    workspace: str,
    cfg: Config,
    instruction: str,
    emit,
    lessons_text: str = "",
    max_rounds: int = 20,
    lang=None,
):
    """Run scoped iterate on a single module."""
    from .codemap import ModuleCodeMapBuilder
    from .quality import build_iterate_prompt

    mod = modular_plan.get_module(module_name)
    if not mod:
        emit("log", msg=f"[ITERATE] Module '{module_name}' not found")
        return

    module_path = mod.path.rstrip("/")
    manifest = FileManifest()

    # Rebuild manifest for module files only
    exts = tuple(lang.extensions) if lang else (".py",)
    skip_dirs = {"__pycache__", ".venv", "node_modules"}
    module_dir = os.path.join(workspace, module_path)
    if os.path.isdir(module_dir):
        for root, _, files in os.walk(module_dir):
            if any(sd in root for sd in skip_dirs):
                continue
            for fname in files:
                if not fname.endswith(exts):
                    continue
                path = os.path.relpath(os.path.join(root, fname), workspace)
                with open(os.path.join(root, fname)) as f:
                    content = f.read()
                manifest.record(path, content)

    # Module validation
    results = run_module_validation(workspace, module_path, mod.test_file, lang=lang)
    failures_text = format_failures(results)

    # Build module code map
    code_map_builder = ModuleCodeMapBuilder(workspace, module_path, budget_tokens=20000, lang=lang)
    code_map = code_map_builder.build(failure_text=failures_text)

    interface_stubs = modular_plan.get_dependency_interfaces(module_name)

    iterate_prompt = build_module_build_prompt(
        module_name=module_name,
        module_test=mod.test_file or "",
        interface_stubs=interface_stubs,
        code_map=code_map,
        validation_failures=failures_text,
        lessons_text=lessons_text,
        lang=lang,
    )

    executor = ModuleScopedExecutor(workspace, manifest, module_path,
                                     context_budget=cfg.max_context_tokens)
    executor.build_mode = True
    executor.grant_rewrites(max(len(manifest.files), 10))
    error_tracker = ErrorTracker(manifest)
    progress = Progress(task=f"Iterate: {module_name}", workspace=workspace)

    task_msg = instruction or f"Fix issues in the {module_name} module."
    messages = _build_messages(iterate_prompt, task_msg)

    if failures_text:
        messages.append({"role": "user", "content":
            f"Current failures:\n{failures_text}\nFix these errors."
        })

    no_tool_rounds = 0
    for round_num in range(max_rounds):
        messages = trim_context(messages, max_tokens=cfg.max_context_tokens)

        emit("phase", label=f"ITERATE/{module_name} (R{round_num + 1}/{max_rounds})",
             total_rounds=round_num + 1, n_files=len(manifest.files),
             phase="iterate", round=round_num + 1, budget=max_rounds)

        msg = chat(cfg, messages, emit=emit)

        if not msg.get("tool_calls"):
            messages.append(msg)
            no_tool_rounds += 1
            if no_tool_rounds >= 3:
                break
            if mod.test_file:
                test_cmd = " ".join(lang.test_cmd) + " " + mod.test_file
            else:
                test_cmd = ""
            if test_cmd:
                messages.append({"role": "user", "content": f"Run {test_cmd} to verify."})
            continue

        no_tool_rounds = 0
        n_exec, _, error_intervention, last_run = process_tool_calls(
            msg, messages, executor, error_tracker, None, progress, emit=emit,
        )

        if error_intervention:
            messages.append({"role": "user", "content": error_intervention})

        if last_run:
            cmd_str, cmd_result = last_run
            if ("pytest" in cmd_str and cmd_result.get("exit_code") == 0):
                emit("log", msg=f"[ITERATE/{module_name}] Tests pass!")
                break

    emit("log", msg=f"[ITERATE/{module_name}] Done")


# ── Main engine ───────────────────────────────────────────────────────────────

def run(task: str, workspace: str, cfg: Config, emitter: EventEmitter | None = None,
        parallel: bool = False):
    """Run the full build pipeline for a task."""
    if emitter is None:
        emitter = EventEmitter()
        emitter.on(default_print_handler)
    emit = emitter.emit

    os.makedirs(workspace, exist_ok=True)
    _git_init(workspace)
    build_log = BuildLogger(workspace)
    emitter.on(build_log.handler)

    # Graceful shutdown on SIGTERM — lets the phase loop's try/except unwind
    # so the summary block still runs (phase-history recording, build_log
    # close). Without this, `pkill` or a systemd stop would lose the partial
    # build's history, which is exactly when future budgets need to scale up.
    import signal
    _orig_sigterm = signal.getsignal(signal.SIGTERM)
    def _sigterm_handler(signum, frame):
        raise KeyboardInterrupt("SIGTERM received")
    try:
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except (ValueError, OSError):
        # signal() only works on the main thread; some callers (parallel
        # module waves) may invoke run() from a worker. Skip silently.
        _orig_sigterm = None

    # Detect target language from task description
    lang = detect_language(task, workspace)

    # Auto-detect model
    if not cfg.model:
        cfg.model = detect_model(cfg)
    emit("info", msg=f"Model: {cfg.model}")
    emit("info", msg=f"Language: {lang.name}")
    emit("info", msg=f"Workspace: {workspace}")
    emit("info", msg=f"API: {cfg.api_url}")
    emit("separator", char="═", width=60)

    # Initialize state
    state = PhaseState()
    progress = Progress(task=task, workspace=workspace)
    manifest = FileManifest()
    error_tracker = ErrorTracker(manifest)
    executor = ToolExecutor(workspace, manifest, progress_fn=progress.to_context,
                            context_budget=cfg.max_context_tokens)
    executor.scratch = Scratch(workspace)
    executor.phase_name = "init"
    executor.round_num = 0
    batch_tracker = None
    plan = None
    modular_plan: ModularPlan | None = None
    modular_manifest_failures = 0
    modular_validation_errors: list[str] = []
    replan_count = 0
    replan_hint = ""
    architecture_text = ""
    review_issues = ""  # Issues found during CRITIC, passed to BUILD
    critic_findings: list[dict] = []  # Structured findings from CRITIC phase
    entry_point = lang.entry_point
    messages: list[dict] = []
    no_tool_rounds = 0
    last_status_round = -1

    # Smart phase exit fingerprints (rotated per phase)
    phase_error_fingerprints: list[str] = []
    last_fingerprint_phase: str = ""

    # Cache of base system prompt for current phase (for scratch refresh)
    current_phase_system_prompt: str = ""

    # Track when reflection was last run per phase (so we don't spam)
    reflection_run_for_phase: set = set()

    # Track recent failure context for failure reflection
    last_failures_summary: str = ""

    # Inspector commissioning output (set after modular SCAFFOLD, consumed by INTEGRATE).
    # Held as a list of Violation so we can preserve structure (severity, rule, etc.)
    # through to the LLM.
    commissioning_violations: list = []

    # Load persistent memory
    lessons = recall(task)
    lessons_text = format_for_prompt(lessons)
    task_tags = list(infer_task_tags(task))
    if lessons_text:
        emit("log", msg=f"[MEMORY] Loaded {len(lessons)} lessons from past builds (tags={task_tags})")
        progress.lessons_applied = [l.trigger for l in lessons]

    t_start = time.time()

    # Lazy code map — built after SCAFFOLD when files exist
    from .codemap import CodeMapBuilder
    _code_map_builder: CodeMapBuilder | None = None
    _code_map_stale = True  # True when files edited since last code map build

    _last_failure_text = ""

    def _get_code_map(failure_text: str = "") -> str:
        nonlocal _code_map_builder, _code_map_stale, _last_failure_text
        if _code_map_builder is None:
            budget = max(cfg.max_context_tokens - 10000, 10000)
            _code_map_builder = CodeMapBuilder(workspace, budget_tokens=budget, lang=lang)
            _code_map_stale = True
        # Return cached if files unchanged and same failure context
        if (not _code_map_stale and failure_text == _last_failure_text
                and hasattr(_code_map_builder, '_last_output') and _code_map_builder._last_output):
            return _code_map_builder._last_output
        result = _code_map_builder.build(failure_text=failure_text)
        _code_map_stale = False
        _last_failure_text = failure_text
        return result

    # ── Phase loop ──
    try:
        while True:
            if not state.tick():
                if state.current in (Phase.PLAN, Phase.DEPS):
                    emit("log", msg=f"[ABORT] Failed in {state.current.value} phase after {state.round_in_phase} rounds")
                    _run_phase_reflection(
                        cfg, state.current.value, task, manifest, progress, lessons, emit,
                        failures=last_failures_summary or f"aborted in {state.current.value} phase",
                        success=False, tags=task_tags,
                    )
                    break
                emit("log", msg=f"[BUDGET] {state.current.value} phase exhausted, advancing")
                if state.advance() is None:
                    break

            # Global round cap -- prevent runaway loops in modular pipeline
            if state.is_over_budget():
                emit("log", msg=f"[ABORT] Global round limit ({state.max_total_rounds}) reached at R{state.total_rounds}")
                _run_phase_reflection(
                    cfg, "GLOBAL_BUDGET", task, manifest, progress, lessons, emit,
                    failures=last_failures_summary or "global round limit reached",
                    success=False, tags=task_tags,
                )
                break

            progress.set_phase(state)

            # Sync executor phase context (for note_lesson timestamps)
            executor.phase_name = state.current.value
            executor.round_num = state.round_in_phase

            # Reset per-phase smart-exit fingerprints when phase changes
            if last_fingerprint_phase != state.current.value:
                phase_error_fingerprints = []
                last_fingerprint_phase = state.current.value

            # Checkpoint at the start of each new phase
            if state.round_in_phase == 1:
                _save_checkpoint(workspace, state, manifest, plan, architecture_text, entry_point, task)
                _git_checkpoint(workspace, state.current.value)

            emit("phase", label=state.phase_label, total_rounds=state.total_rounds,
                 n_files=len(manifest.files), phase=state.current.value,
                 round=state.round_in_phase, budget=state.max_rounds.get(state.current, 60))

            # ── PLAN phase (round 1: architecture, round 2+: manifest) ──
            if state.current == Phase.PLAN:
                if not architecture_text:
                    emit("log", msg="[PLAN] Designing architecture...")
                    # Detect if modular architecture is needed
                    use_modular = _should_use_modular(task)
                    if use_modular:
                        arch_prompt = build_modular_architecture_prompt(lessons_text, lang=lang)
                        emit("log", msg="[PLAN] Using modular architecture prompt")
                    else:
                        arch_prompt = build_architecture_prompt(lessons_text, lang=lang)
                    if replan_hint:
                        arch_prompt += replan_hint
                        replan_hint = ""
                        emit("log", msg="[PLAN] Re-planning with failure context from previous attempt")
                    msgs = _build_messages(arch_prompt, task)
                    msg = chat(cfg, msgs, tools=[], emit=emit)
                    architecture_text = (msg.get("content") or "").strip()

                    if architecture_text and len(architecture_text) > 50:
                        # Re-check for modular after seeing architecture
                        if not use_modular and _should_use_modular(architecture_text):
                            emit("log", msg="[PLAN] Architecture suggests modular project, re-generating...")
                            arch_prompt = build_modular_architecture_prompt(lessons_text, lang=lang)
                            msgs = _build_messages(arch_prompt, task)
                            msg = chat(cfg, msgs, tools=[], emit=emit)
                            architecture_text = (msg.get("content") or "").strip()

                        with open(os.path.join(workspace, "architecture.md"), "w") as f:
                            f.write(architecture_text)
                        progress.log("Architecture designed")
                        emit("log", msg=f"[Architecture: {len(architecture_text)} chars]")
                    else:
                        emit("log", msg="[Architecture too short, retrying...]")
                        architecture_text = ""
                else:
                    # Round 2+: Generate manifest from architecture
                    is_modular_arch = _should_use_modular(architecture_text)
                    modular_fallback_to_flat = modular_manifest_failures >= 2
                    if is_modular_arch and not modular_fallback_to_flat:
                        emit("log", msg="[PLAN] Building MODULAR manifest from architecture...")
                        manifest_prompt = build_modular_manifest_prompt(architecture_text, lessons_text, lang=lang)
                        if modular_validation_errors:
                            error_feedback = (
                                "\n\n## PREVIOUS ATTEMPT FAILED VALIDATION\n"
                                "Your previous manifest had these errors — you MUST fix them:\n"
                                + "\n".join(f"- {e}" for e in modular_validation_errors)
                                + "\n\nPay special attention to depends_on fields. "
                                "A module CANNOT depend on a module that depends back on it."
                            )
                            manifest_prompt += error_feedback
                    else:
                        if modular_fallback_to_flat and is_modular_arch:
                            emit("log", msg="[PLAN] Modular manifest failed twice, falling back to flat...")
                        else:
                            emit("log", msg="[PLAN] Building manifest from architecture...")
                        manifest_prompt = build_manifest_prompt(architecture_text, lessons_text, lang=lang)
                    msgs = _build_messages(manifest_prompt, task)
                    msg = chat(cfg, msgs, tools=[], emit=emit)

                    text = (msg.get("content") or "").strip()
                    plan = extract_json(text)

                    if plan and isinstance(plan, dict):
                        # Modular plan handling
                        if plan.get("modular") and plan.get("modules"):
                            validation_errors = validate_modular_plan(plan)
                            if validation_errors:
                                modular_manifest_failures += 1
                                modular_validation_errors = validation_errors
                                emit("log", msg=f"[Modular plan validation failed ({modular_manifest_failures}/2): {'; '.join(validation_errors[:3])}]")
                                if modular_manifest_failures >= 2:
                                    emit("log", msg="[Will fall back to flat manifest on next round]")
                                else:
                                    emit("log", msg="[Retrying with error feedback...]")
                                plan = None
                                continue

                            modular_plan = ModularPlan.from_dict(plan, lang=lang)
                            # Convert to flat plan for backward-compat consumers
                            flat_plan = modular_plan.to_flat_plan()
                            _inject_plan_constraints(plan, executor.scratch, emit)
                            n_files = len(flat_plan.get("files", []))
                            n_deps = len(flat_plan.get("dependencies", []))
                            n_modules = len(modular_plan.modules)
                            entry_point = modular_plan.entry_point
                            emit("log", msg=f"[MODULAR Manifest: {n_modules} modules, {n_files} files, {n_deps} deps, entry: {entry_point}]")

                            with open(os.path.join(workspace, "plan.json"), "w") as f:
                                json.dump(plan, f, indent=2)

                            # Use flat plan for batch tracker and progress
                            batch_tracker = BatchTracker(
                                flat_plan,
                                run_cmd=lang.run_cmd if lang else "python3",
                                entry_point=entry_point,
                            )
                            progress.set_plan(flat_plan)
                            progress.log(f"Planned {n_modules} modules, {n_files} files, {n_deps} deps")

                            budgets = compute_budgets(flat_plan, task_text=task)
                            state.max_rounds.update(budgets)
                            state.max_total_rounds = budgets.get("max_total_rounds", 1000)
                            emit("log", msg=f"[Budgets: SCAFFOLD={budgets[Phase.SCAFFOLD]}, BUILD={budgets[Phase.BUILD]}, INTEGRATE={budgets[Phase.INTEGRATE]}, MAX_TOTAL={state.max_total_rounds}]")

                            state.advance()  # -> DEPS
                        elif "files" in plan:
                            # Standard flat plan
                            # Tier 0 — blueprint verification: downgrade react→typescript if
                            # the detected language doesn't match the plan's actual contents.
                            verified_lang = inspector.verify_language_against_plan(lang, plan)
                            if verified_lang is not lang:
                                emit("log", msg=(
                                    f"[INSPECTOR blueprint] downgrading {lang.name} → "
                                    f"{verified_lang.name} — plan has no JSX/React usage"
                                ))
                                lang = verified_lang
                            n_files = len(plan.get("files", []))
                            n_deps = len(plan.get("dependencies", []))
                            entry_point = plan.get("entry_point", lang.entry_point)
                            emit("log", msg=f"[Manifest: {n_files} files, {n_deps} deps, entry: {entry_point}]")

                            with open(os.path.join(workspace, "plan.json"), "w") as f:
                                json.dump(plan, f, indent=2)

                            _inject_plan_constraints(plan, executor.scratch, emit)

                            batch_tracker = BatchTracker(
                                plan,
                                run_cmd=lang.run_cmd if lang else "python3",
                                entry_point=entry_point,
                            )
                            progress.set_plan(plan)
                            progress.log(f"Planned {n_files} files, {n_deps} deps")

                            # Dynamic budgets based on task complexity + past builds
                            budgets = compute_budgets(plan, task_text=task)
                            state.max_rounds.update(budgets)
                            state.max_total_rounds = budgets.get("max_total_rounds", 500)
                            emit("log", msg=f"[Budgets: SCAFFOLD={budgets[Phase.SCAFFOLD]}, BUILD={budgets[Phase.BUILD]}, MAX_TOTAL={state.max_total_rounds}]")

                            state.advance()  # -> DEPS
                        else:
                            emit("log", msg="[Invalid manifest, retrying...]")
                            progress.log("Manifest parse failed, retrying")
                    else:
                        emit("log", msg="[Invalid manifest, retrying...]")
                        progress.log("Manifest parse failed, retrying")

            # ── DEPS phase ──
            elif state.current == Phase.DEPS:
                deps = plan.get("dependencies", []) if plan else []

                # Node family needs package.json + tsconfig.json + framework config BEFORE
                # scaffold, regardless of whether the plan declared deps. Plan LLMs often
                # under-declare TS deps (treating vitest/typescript as "built in"); without
                # this unconditional write the project has no tsconfig and the lockdown then
                # prevents the LLM from creating one — a hard deadlock.
                if lang and lang.family == "node":
                    # Full-stack plans route their entry_point through a subdir
                    # (e.g. "frontend/src/main.tsx"). Honor that — writing
                    # package.json + index.html at root while the LLM puts React
                    # code in frontend/ leaves vite with dangling references.
                    entry = plan.get("entry_point", "") if plan else ""
                    node_subdir = ""
                    if entry and "/" in entry:
                        first = entry.split("/", 1)[0]
                        # Only treat well-known frontend-project dir names as
                        # subdir targets — avoid false positives on "src/..."
                        # or deep paths.
                        if first in ("frontend", "client", "web", "app", "ui"):
                            node_subdir = first
                    target_base = os.path.join(workspace, node_subdir) if node_subdir else workspace
                    pkg_json = os.path.join(target_base, "package.json")
                    tsconfig_path = os.path.join(target_base, "tsconfig.json")
                    if not os.path.exists(pkg_json) or not os.path.exists(tsconfig_path):
                        _write_node_boilerplate(workspace, deps, lang, subdir=node_subdir)
                        loc = f"{node_subdir}/" if node_subdir else "root"
                        emit("log", msg=f"[Wrote node boilerplate to {loc}: package.json + tsconfig.json + framework config]")

                # For Python projects, write a minimal entry-point stub at plan.entry_point
                # so the LLM edits IT during SCAFFOLD rather than inventing a different entry
                # in cli/main.py and ignoring the inspector's "entry_point_missing" warning.
                # This is the same idea as the node boilerplate: foundation files exist so the
                # LLM builds on them, and the inspector doesn't have to nag later.
                if lang and lang.family == "python":
                    entry = plan.get("entry_point") if plan else None
                    if entry and _write_python_entry_stub(workspace, entry):
                        emit("log", msg=f"[Wrote python entry stub at {entry} — LLM should fill in main()]")

                if not deps:
                    emit("log", msg="[No dependencies, skipping]")
                    state.advance()  # -> SCAFFOLD
                    continue

                emit("log", msg=f"[Installing {len(deps)} dependencies: {', '.join(deps)}]")
                all_ok = True

                if lang.family == "static":
                    # Static sites (HTML/CSS/JS): no package manager — skip all deps
                    emit("log", msg=f"[{lang.name} project — no package manager, skipping deps]")
                    state.advance()
                    continue

                if lang.family == "node":
                    # Boilerplate already written above; just install. When the
                    # boilerplate landed in a subdir (node_subdir set earlier),
                    # run `npm install` from THERE, not from workspace root
                    # where no package.json exists. Otherwise npm errors ENOENT
                    # and cadillac keeps retrying on an empty root.
                    install_cmd = f"cd {node_subdir} && npm install" if node_subdir else "npm install"
                    result = executor.run_command(install_cmd)
                    if result.get("exit_code", 1) == 0:
                        for dep in deps:
                            progress.mark_dep_done(dep)
                        emit("log", msg=f"  [OK] npm install ({len(deps)} packages)")
                    else:
                        emit("log", msg=f"  [FAIL] npm install: {result.get('stderr', result.get('error', ''))[:200]}")
                        all_ok = False
                else:
                    for dep in deps:
                        result = executor.run_command(_pip_install_cmd(dep, workspace))
                        if result.get("exit_code", 1) == 0:
                            progress.mark_dep_done(dep)
                            emit("log", msg=f"  [OK] {dep}")
                        else:
                            emit("log", msg=f"  [FAIL] {dep}: {result.get('stderr', result.get('error', ''))[:200]}")
                            all_ok = False
                            progress.log(f"Dep install failed: {dep}")

                if all_ok or state.round_in_phase >= 2:
                    # Tier 1 inspection — catch host-unsafe dep versions (e.g. vitest:*)
                    # before SCAFFOLD starts wiring code around them.
                    _run_inspector("materials", workspace, lang, plan, emit)
                    state.advance()  # -> SCAFFOLD
                    progress.log(f"Dependencies {'all installed' if all_ok else 'partially installed'}")

            # ── SCAFFOLD phase ──
            elif state.current == Phase.SCAFFOLD:
                # Modular path: build all modules via waves, then skip to INTEGRATE
                if modular_plan and modular_plan.is_modular() and state.round_in_phase == 1:
                    emit("log", msg=f"[MODULAR] Building {len(modular_plan.modules)} modules in {len(modular_plan.module_build_order)} wave(s)")
                    all_failed = set()
                    degraded = set()  # modules whose deps failed but we'll try anyway
                    # Aggregate per-module rounds across all waves so they can be
                    # rolled into state.phase_rounds_used for cross-build history.
                    modular_rounds_total = {"scaffold": 0, "build": 0}
                    for wave_idx, wave in enumerate(modular_plan.module_build_order):
                        runnable = [m for m in wave if m not in all_failed]
                        for m in runnable:
                            mod = modular_plan.get_module(m)
                            if mod and any(d in all_failed for d in mod.depends_on):
                                degraded.add(m)
                        if degraded & set(runnable):
                            emit("log", msg=f"[WAVE {wave_idx + 1}] Attempting {degraded & set(runnable)} despite dependency failures")

                        if not runnable:
                            continue

                        emit("log", msg=f"[WAVE {wave_idx + 1}/{len(modular_plan.module_build_order)}] Building: {', '.join(runnable)}")
                        results, wave_rounds = _build_module_wave(
                            runnable, modular_plan, workspace, cfg, manifest,
                            emit, lessons_text, architecture_text, parallel=parallel,
                            lang=lang,
                        )
                        for k in modular_rounds_total:
                            modular_rounds_total[k] += wave_rounds.get(k, 0)
                        failed = {n for n, ok in results.items() if not ok}
                        if failed:
                            emit("log", msg=f"[WAVE {wave_idx + 1}] Failed: {', '.join(failed)}")
                            all_failed.update(failed)

                    # Bump state.phase_rounds_used so end-of-run phase-history
                    # sees the REAL modular work (not the 1 top-level round that
                    # advance()'s snapshot would otherwise record).
                    if modular_rounds_total["scaffold"]:
                        state.phase_rounds_used[Phase.SCAFFOLD] = (
                            state.phase_rounds_used.get(Phase.SCAFFOLD, 0)
                            + modular_rounds_total["scaffold"]
                        )
                    if modular_rounds_total["build"]:
                        state.phase_rounds_used[Phase.BUILD] = (
                            state.phase_rounds_used.get(Phase.BUILD, 0)
                            + modular_rounds_total["build"]
                        )
                    emit("log", msg=f"[MODULAR] Rolled up per-module rounds: scaffold={modular_rounds_total['scaffold']}, build={modular_rounds_total['build']}")

                    if all_failed:
                        emit("log", msg=f"[MODULAR] {len(all_failed)} module(s) failed: {', '.join(sorted(all_failed))}")

                    # Tier 3 inspection — the commissioning gate. Per-module tests can pass
                    # while a cross-module import/type error still breaks the workspace.
                    # Inspector handles that + the Tier 2 wiring audit.
                    commissioning_violations = _run_inspector(
                        "commissioning", workspace, lang, plan, emit,
                    )
                    _run_inspector("wiring", workspace, lang, plan, emit)

                    # Skip BUILD, go to INTEGRATE (REVIEW runs after INTEGRATE)
                    state.current = Phase.INTEGRATE
                    state.round_in_phase = 0
                    messages = []
                    no_tool_rounds = 0
                    continue

                if not messages or state.round_in_phase == 1:
                    scaffold_prompt = build_scaffold_prompt(
                        architecture=architecture_text,
                        manifest_summary=manifest.to_detailed(),
                        progress_context=progress.to_context(),
                        lessons_text=lessons_text,
                        lang=lang,
                    )
                    messages = _build_messages(scaffold_prompt, task)

                    if batch_tracker:
                        parts = []
                        deps = plan.get("dependencies", []) if plan else []
                        if deps:
                            parts.append(f"Dependencies already installed: {', '.join(deps)}")
                            parts.append("")
                        directive = batch_tracker.get_current_directive()
                        if directive:
                            parts.append(directive)
                        if parts:
                            messages.append({"role": "user", "content": "\n".join(parts)})

                messages = trim_context(messages, max_tokens=cfg.max_context_tokens)

                if state.round_in_phase > 1 and state.round_in_phase % 5 == 0 and state.round_in_phase != last_status_round:
                    messages.append({"role": "user", "content": f"[STATUS] {progress.to_context()}\n{manifest.to_status()}"})
                    last_status_round = state.round_in_phase

                msg = chat(cfg, messages, emit=emit)

                if not msg.get("tool_calls"):
                    content = msg.get("content", "")
                    messages.append(msg)

                    # Try to extract code from markdown fences and write them
                    extracted = _extract_code_fences(content)

                    # Fallback: unnamed code fence + known current batch file
                    if not extracted and batch_tracker:
                        raw_fences = re.findall(r'```(?:python|typescript|javascript|ts|js)?\s*\n(.*?)```', content, re.DOTALL)
                        if raw_fences:
                            cur_batch = batch_tracker.build_order[batch_tracker.current_batch_idx] \
                                if batch_tracker.current_batch_idx < len(batch_tracker.build_order) else None
                            if cur_batch:
                                expected_file = cur_batch["files"][0]
                                if expected_file not in batch_tracker.written_files:
                                    # Use the longest fence (most likely the real code)
                                    best = max(raw_fences, key=len).strip()
                                    if len(best.splitlines()) >= 5:  # Sanity: at least 5 lines
                                        extracted = [(expected_file, best)]
                                        emit("log", msg=f"[Inferred filename {expected_file} for unnamed code fence]")

                    if extracted:
                        for path, code in extracted:
                            result = executor.write_file(path, code)
                            if result.get("status") == "ok":
                                emit("log", msg=f"[Extracted {path} from markdown ({result.get('lines', '?')} lines)]")
                        no_tool_rounds = 0  # Made progress
                        messages.append({"role": "user", "content":
                            "I extracted the code you wrote. Next file — use write_file() as a tool call."
                        })
                        continue

                    no_tool_rounds += 1
                    if no_tool_rounds >= 3:
                        emit("log", msg="[Stuck in scaffold, forcing advance]")
                        state.advance()  # -> REVIEW
                        messages = []
                        no_tool_rounds = 0
                        continue
                    nudge = batch_tracker.get_remaining_nudge() if batch_tracker else None
                    if not nudge:
                        nudge = "Do NOT output code in markdown. Call write_file(path, content) as a tool call."
                    messages.append({"role": "user", "content": nudge})
                    continue

                no_tool_rounds = 0
                n_exec, batch_nudge, error_intervention, _ = process_tool_calls(
                    msg, messages, executor, error_tracker, batch_tracker, progress, emit=emit,
                )

                if error_intervention:
                    messages.append({"role": "user", "content": error_intervention})
                elif batch_nudge:
                    emit("log", msg=f"[BATCH] {batch_nudge[:80]}...")
                    messages.append({"role": "user", "content": batch_nudge})

                if batch_tracker and batch_tracker.all_done:
                    progress.log("All planned files written")
                    # Post-scaffold: strip .js extensions from TS imports
                    if lang and lang.family == "node":
                        n = _strip_js_extensions_from_ts(workspace, manifest)
                        if n:
                            emit("log", msg=f"[Post-scaffold] Stripped .js extensions from {n} TS files")
                        if _protect_tsconfig(workspace, lang):
                            emit("log", msg="[Post-scaffold] Restored tsconfig.json critical settings")
                        if _protect_package_json(workspace, lang):
                            emit("log", msg="[Post-scaffold] Removed 'type: module' from package.json")
                    # Tier 2 inspection — scripts, configs, entry_point self-consistent?
                    # Errors surface on the next BUILD round; defense-in-depth with _protect_*.
                    _run_inspector("wiring", workspace, lang, plan, emit)
                    # Lock protected configs — only add_dep() and harness safeguards touch them after this point.
                    executor.config_writes_allowed = False
                    state.advance()  # -> REVIEW
                    messages = []
                    no_tool_rounds = 0

            # ── CRITIC phase (adversarial review, structured findings passed to BUILD) ──
            elif state.current == Phase.REVIEW:
                executor.build_mode = True  # Let LLM read full file contents
                if not messages or state.round_in_phase == 1:
                    emit("log", msg="[CRITIC] Adversarial code review...")
                    code_map = _get_code_map()
                    emit("log", msg=f"[Code map: tier {_code_map_builder.tier}, {_code_map_builder.last_tokens} tokens]")
                    review_prompt = build_review_prompt(
                        entry_point=entry_point,
                        manifest_summary=manifest.to_detailed(),
                        architecture=architecture_text,
                        code_map=code_map,
                        lang=lang,
                    )
                    messages = _build_messages(review_prompt, task)
                    messages.append({"role": "user", "content":
                        "Critically review the code map above. Use read_file to inspect any function "
                        "that looks suspicious. Output a JSON array of findings, or [] if clean. Do NOT edit files."
                    })

                # Only allow read-only tools during CRITIC
                read_only_tools = [t for t in TOOL_DEFS if t["function"]["name"] in ("read_file", "list_files", "search_files", "check_status")]
                msg = chat(cfg, messages, tools=read_only_tools, emit=emit)

                content = (msg.get("content") or "").strip()
                if msg.get("tool_calls"):
                    no_tool_rounds = 0
                    n_exec, _, _, _ = process_tool_calls(
                        msg, messages, executor, error_tracker, None, progress, emit=emit,
                    )
                else:
                    # Try to parse structured JSON findings
                    findings = _parse_critic_findings(content)
                    if findings:
                        # Sort by severity: high first
                        severity_order = {"high": 0, "medium": 1, "low": 2}
                        findings.sort(key=lambda f: severity_order.get(f.get("severity", "low"), 2))
                        critic_findings = findings
                        n_high = sum(1 for f in findings if f.get("severity") == "high")
                        n_med = sum(1 for f in findings if f.get("severity") == "medium")
                        n_low = sum(1 for f in findings if f.get("severity") == "low")
                        emit("log", msg=f"[CRITIC] Found {len(findings)} issues: {n_high} high, {n_med} medium, {n_low} low")
                        progress.log(f"Critic found {len(findings)} issues")
                        # Also save as text for backward compat
                        review_issues = _format_critic_findings(findings)
                    elif content and ("[]" in content or "LGTM" in content.upper() or "no issues" in content.lower()):
                        emit("log", msg="[CRITIC] No issues found, advancing to build")
                        progress.log("Critic review passed")
                        state.advance()  # -> BUILD
                        messages = []
                        no_tool_rounds = 0
                        continue
                    else:
                        # Unstructured response — treat as free-text findings (fallback)
                        if content and len(content) > 20:
                            review_issues = content
                            emit("log", msg=f"[CRITIC] Unstructured findings ({len(content)} chars)")
                            progress.log("Critic found issues (unstructured)")
                    messages.append(msg)

                # After max rounds, advance with any collected findings
                if state.round_in_phase >= state.max_rounds.get(Phase.REVIEW, 4):
                    progress.log("Critic review complete")
                    state.advance()  # -> BUILD
                    messages = []
                    no_tool_rounds = 0

            # ── BUILD phase ──
            elif state.current == Phase.BUILD:
                executor.build_mode = True
                executor.config_writes_allowed = False
                if not messages or state.round_in_phase == 1:
                    _edits_without_test = 0
                    _build_read_count = 0
                    code_map = _get_code_map()
                    emit("log", msg=f"[Code map: tier {_code_map_builder.tier}, {_code_map_builder.last_tokens} tokens]")
                    build_prompt = build_build_prompt(
                        entry_point=entry_point,
                        manifest_summary=manifest.to_detailed(),
                        progress_context=progress.to_context(),
                        lessons_text=lessons_text,
                        code_map=code_map,
                        lang=lang,
                    )
                    current_phase_system_prompt = build_prompt
                    messages = _build_messages(build_prompt, task,
                                               scratch_text=executor.scratch.read())
                    # Inject live pinned versions as a reality anchor — memory lessons can
                    # claim "X needs newer Y" when Y is actually already safely pinned.
                    pinned_block = _pinned_versions_block(workspace, lang)
                    if pinned_block:
                        messages.append({"role": "user", "content": pinned_block})
                    build_intro = f"All files are written. Run `{lang.run_cmd} {entry_point} --test` now to test."
                    if review_issues:
                        build_intro += f"\n\nREVIEW ISSUES (fix these first):\n{review_issues}"
                        review_issues = ""  # Don't inject again on retry
                    messages.append({"role": "user", "content": build_intro})

                messages = trim_context(messages, max_tokens=cfg.max_context_tokens)

                # Refresh scratch in system prompt every 3 rounds (cheap; survives compression)
                if current_phase_system_prompt and state.round_in_phase > 1 and state.round_in_phase % 3 == 0:
                    _refresh_scratch_in_messages(messages, current_phase_system_prompt, executor.scratch.read())

                if state.round_in_phase > 1 and state.round_in_phase % 5 == 0 and state.round_in_phase != last_status_round:
                    # Refresh code map only if files were edited since last build
                    if _code_map_builder is not None and _code_map_stale:
                        _get_code_map()
                        emit("log", msg=f"[Code map refreshed: tier {_code_map_builder.tier}, {_code_map_builder.last_tokens} tokens]")
                    messages.append({"role": "user", "content": f"[STATUS] {progress.to_context()}\n{manifest.to_status()}"})
                    last_status_round = state.round_in_phase

                # FIX-mode: after retreat_to_build, restrict tools until first edit lands.
                # This removes run_command as an option so the LLM can't just re-run the entry point
                # and ignore the validation failures.
                if state.fix_required:
                    build_tools = [t for t in TOOL_DEFS if t["function"]["name"] != "run_command"]
                else:
                    build_tools = None  # default TOOL_DEFS

                msg = chat(cfg, messages, tools=build_tools, emit=emit)

                if not msg.get("tool_calls"):
                    messages.append(msg)
                    content = (msg.get("content") or "").strip()
                    if content and len(content) > 20 and state.round_in_phase > 2:
                        emit("log", msg="[Model indicates completion, advancing to validation]")
                        state.advance()  # -> VALIDATE
                        messages = []
                        no_tool_rounds = 0
                        continue

                    no_tool_rounds += 1
                    if no_tool_rounds >= 3:
                        state.advance()  # -> VALIDATE
                        messages = []
                        no_tool_rounds = 0
                        continue
                    messages.append({"role": "user", "content":
                        f"Run `{lang.run_cmd} {entry_point} --test` to test the code. Use run_command now."
                    })
                    continue

                no_tool_rounds = 0

                # Track tool types for nudges (Fix 2 + 3)
                _has_run = any(
                    tc["function"]["name"] == "run_command"
                    for tc in msg.get("tool_calls", [])
                )
                _n_reads = sum(1 for tc in msg.get("tool_calls", []) if tc["function"]["name"] == "read_file")
                _build_read_count += _n_reads

                # Snapshot manifest state so we can tell whether any edit SUCCEEDED
                # (vs was rejected by guards like _config_guard or _scope_check).
                _pre_versions = {p: info["version"] for p, info in manifest.files.items()}

                n_exec, batch_nudge, error_intervention, last_run = process_tool_calls(
                    msg, messages, executor, error_tracker, None, progress, emit=emit,
                )

                # _has_edit means "a file was actually written this round", not "LLM attempted
                # to write". Guards (protected config, scope check) reject some calls; those
                # don't count as edits for nudges or FIX_MODE clearing.
                _has_edit = any(
                    info["version"] > _pre_versions.get(p, 0)
                    for p, info in manifest.files.items()
                )

                # Per-round safeguards (idempotent; runs behind executor config lockdown).
                _post_round_safeguards(workspace, lang, manifest, messages, msg, emit)

                # Fix-mode lifecycle: delegate to PhaseState.tick_fix_mode so the engine
                # and unit tests share the SAME logic (no divergence).
                fix_transition = state.tick_fix_mode(had_successful_edit=_has_edit)
                if fix_transition == "cleared":
                    emit("log", msg="[FIX_MODE] edit applied — run_command re-enabled next round")
                elif fix_transition == "fallback":
                    emit("log", msg=(
                        "[FIX_MODE] fallback — 3 rounds with zero successful edits; "
                        "re-enabling run_command so LLM can probe state"
                    ))
                    messages.append({"role": "user", "content": (
                        "FIX_MODE FALLBACK: the harness re-enabled run_command because your "
                        "edits weren't landing. Try a different file or read the failing file "
                        "fresh to see its current state before your next edit."
                    )})

                # Track error fingerprints for smart phase exit + causation tracking
                if last_run:
                    _, _cmd_result = last_run
                    # Test runners (vitest, pytest) write failures to stdout, not stderr.
                    # Use whichever is non-empty when exit code is nonzero.
                    _stderr = (_cmd_result.get("stderr") or "").strip()
                    _stdout = (_cmd_result.get("stdout") or "").strip()
                    _err_text = _stderr if _stderr else _stdout
                    if _cmd_result.get("exit_code", 0) != 0 and _err_text:
                        fp = _error_fingerprint(_err_text)
                        if fp:
                            phase_error_fingerprints.append(fp)
                            last_failures_summary = _err_text[:500]
                            # Causation: penalize lessons whose triggers appear in the fresh error
                            applied = list(progress.lessons_applied or [])
                            if applied:
                                backfired = penalize_backfired(applied, _err_text)
                                for trig in backfired:
                                    emit("log", msg=f"[MEMORY] applied_lesson_backfired: {trig[:60]}")

                # Smart phase exit: 3-of-5 repeated errors -> bail
                if _is_phase_stuck(phase_error_fingerprints):
                    emit("log", msg="[PHASE_STUCK] BUILD repeating same error — exiting phase early")
                    progress.log("BUILD stuck on recurring error, advancing to VALIDATE")
                    state.advance()  # -> VALIDATE
                    messages = []
                    no_tool_rounds = 0
                    continue

                # Fix 2: Edit-then-test nudge
                if _has_edit and not _has_run:
                    _edits_without_test += 1
                elif _has_run:
                    _edits_without_test = 0
                if _edits_without_test >= 2:
                    emit("log", msg=f"[Nudge: {_edits_without_test} edits without testing]")
                    messages.append({"role": "user", "content":
                        f"You've made {_edits_without_test} edits without testing. "
                        f"Run `{lang.run_cmd} {entry_point} --test` NOW to check your changes."
                    })
                    _edits_without_test = 0

                # Fix 3: Read budget nudge
                if _build_read_count >= 10 and _n_reads > 0 and _build_read_count - _n_reads < 10:
                    emit("log", msg=f"[BUILD read budget exhausted ({_build_read_count} reads)]")
                    messages.append({"role": "user", "content":
                        "You've used your read_file budget. Use the CODE MAP in the system prompt for reference. "
                        "Focus on editing code and running tests."
                    })

                if error_intervention:
                    messages.append({"role": "user", "content": error_intervention})

                # Fix 4: Mark code map stale after edits
                if _has_edit:
                    _code_map_stale = True

                # Dynamic re-planning: if multiple distinct error classes + halfway through budget, re-architect
                build_budget = state.max_rounds.get(Phase.BUILD, 60)
                should_replan = (
                    error_tracker.distinct_error_classes() >= 3
                    and state.round_in_phase >= build_budget // 2
                    and replan_count < 1
                )
                if should_replan:
                    replan_count += 1
                    failure_summary = error_tracker.summary()
                    emit("log", msg=f"[REPLAN] Build failing across {error_tracker.distinct_error_classes()} error classes: {failure_summary}")
                    emit("log", msg="[REPLAN] Re-architecting from scratch...")
                    progress.log(f"Re-planning: {failure_summary}")

                    replan_hint = (
                        f"\n\n## PREVIOUS ATTEMPT FAILED\n"
                        f"The previous architecture produced these errors during build:\n{failure_summary}\n"
                        f"The original architecture was:\n{architecture_text[:2000]}\n"
                        f"Redesign the architecture to avoid these issues. Simplify where possible."
                    )
                    architecture_text = ""

                    state.retreat_to_plan()
                    error_tracker.reset()
                    messages = []
                    no_tool_rounds = 0
                    continue

                # Convergence detection: if agent is stuck on same error class, force advance
                if error_tracker.is_stuck():
                    emit("log", msg="Agent stuck on same error class after 3+ interventions — forcing phase advance")
                    progress.log("Stuck on recurring error, advancing to VALIDATE")
                    state.advance()
                    messages = []
                    no_tool_rounds = 0
                    continue

                if last_run:
                    cmd_str, cmd_result = last_run
                    stdout = cmd_result.get("stdout", "")
                    stderr = cmd_result.get("stderr", "")
                    has_pipe = "|" in cmd_str and entry_point not in cmd_str.split("|")[-1]
                    fail_indicators = ("Test failed", "Error:", "FAIL", "Traceback", "AssertionError")
                    stdout_clean = not any(ind in stdout for ind in fail_indicators)
                    if (not has_pipe
                            and entry_point in cmd_str
                            and cmd_result.get("exit_code") == 0
                            and "Traceback" not in stderr
                            and stdout_clean):
                        progress.log("Entry point runs clean")
                        if "BUILD" not in reflection_run_for_phase:
                            _run_phase_reflection(
                                cfg, "BUILD", task, manifest, progress, lessons, emit,
                                failures="", success=True, tags=task_tags,
                            )
                            reflection_run_for_phase.add("BUILD")
                        state.advance()  # -> VALIDATE
                        messages = []
                        no_tool_rounds = 0

            # ── INTEGRATE phase (modular projects only) ──
            elif state.current == Phase.INTEGRATE:
                if not modular_plan or not modular_plan.is_modular():
                    state.advance()  # Skip for flat projects
                    continue

                executor.build_mode = True
                executor.grant_rewrites(max(len(manifest.files), 10))

                if not messages or state.round_in_phase == 1:
                    emit("log", msg="[INTEGRATE] Wiring modules together...")
                    # Pass failure_text so the code map expands the files mentioned in
                    # the most recent failure (e.g., cross-module commissioning errors).
                    # Without this, INTEGRATE renders a uniform skeleton and the LLM must
                    # re-read each suspected file.
                    integ_failures = last_failures_summary
                    if commissioning_violations:
                        integ_failures = (integ_failures + "\n" if integ_failures else "") + \
                            "\n".join(v.detail or v.fix for v in commissioning_violations if v.severity == "error")
                    code_map = _get_code_map(failure_text=integ_failures)
                    emit("log", msg=f"[INTEGRATE] Code map: tier {_code_map_builder.tier}, {_code_map_builder.last_tokens} tokens")

                    module_summaries = _build_module_summaries(modular_plan, manifest)
                    integration_files_str = ", ".join(modular_plan.integration_files)

                    integration_prompt = build_integration_prompt(
                        module_summaries=module_summaries,
                        integration_files=integration_files_str,
                        architecture=architecture_text,
                        entry_point=entry_point,
                        code_map=code_map,
                        lang=lang,
                    )
                    current_phase_system_prompt = integration_prompt

                    # Concatenate all module scratches into integration system prompt.
                    # Caps scale with context window — bigger model = richer handoff.
                    _int_budget = _scratch_budget(
                        cfg.max_context_tokens,
                        len(modular_plan.modules) if modular_plan else 1,
                    )
                    integrate_scratch_parts = [executor.scratch.read(max_chars=_int_budget["root"])]
                    if modular_plan:
                        mod_scratches = Scratch.read_dependencies(
                            workspace, [m.name for m in modular_plan.modules],
                            cap_per=_int_budget["dep"],
                        )
                        if mod_scratches.strip():
                            integrate_scratch_parts.append(mod_scratches)
                    integrate_scratch = "\n\n".join(p for p in integrate_scratch_parts if p.strip())

                    messages = _build_messages(integration_prompt, task, scratch_text=integrate_scratch)
                    if commissioning_violations:
                        block = inspector.render_for_llm(commissioning_violations)
                        if block:
                            messages.append({"role": "user", "content": (
                                "The commissioning inspector flagged these BEFORE INTEGRATE. "
                                "Fix them FIRST — per-module tests passed locally but the workspace "
                                "as a whole has problems.\n\n" + block
                            )})
                        commissioning_violations = []  # consume once
                    messages.append({"role": "user", "content":
                        f"Write the integration files: {integration_files_str}\n"
                        f"Then run: {lang.run_cmd} {entry_point} --test"
                    })

                messages = trim_context(messages, max_tokens=cfg.max_context_tokens)
                msg = chat(cfg, messages, emit=emit)

                if not msg.get("tool_calls"):
                    content = msg.get("content", "")
                    messages.append(msg)
                    # Try code fence extraction
                    extracted = _extract_code_fences(content)
                    if extracted:
                        for path, code in extracted:
                            result = executor.write_file(path, code)
                            if result.get("status") == "ok":
                                emit("log", msg=f"[INTEGRATE] Wrote {path}")
                        _code_map_stale = True
                        messages.append({"role": "user", "content":
                            f"Good. Now run: {lang.run_cmd} {entry_point} --test"})
                        continue

                    no_tool_rounds += 1
                    if no_tool_rounds >= 3:
                        # Route to REVIEW (critic) before VALIDATE
                        state.current = Phase.REVIEW
                        state.round_in_phase = 0
                        messages = []
                        no_tool_rounds = 0
                        continue
                    messages.append({"role": "user", "content":
                        f"Write {integration_files_str} using write_file, then run {lang.run_cmd} {entry_point} --test"
                    })
                    continue

                no_tool_rounds = 0
                n_exec, _, error_intervention, last_run = process_tool_calls(
                    msg, messages, executor, error_tracker, None, progress, emit=emit,
                )

                if error_intervention:
                    messages.append({"role": "user", "content": error_intervention})

                # Mark code map stale after writes
                _has_write = any(
                    tc["function"]["name"] in ("write_file", "edit_file", "line_edit")
                    for tc in msg.get("tool_calls", [])
                )
                if _has_write:
                    _code_map_stale = True

                # Track error fingerprints for smart phase exit
                if last_run:
                    _, _cmd_result = last_run
                    _stderr = (_cmd_result.get("stderr") or "").strip()
                    _stdout = (_cmd_result.get("stdout") or "").strip()
                    _err_text = _stderr if _stderr else _stdout
                    if _cmd_result.get("exit_code", 0) != 0 and _err_text:
                        fp = _error_fingerprint(_err_text)
                        if fp:
                            phase_error_fingerprints.append(fp)
                            last_failures_summary = _stderr[:500]

                # Smart phase exit for INTEGRATE
                if _is_phase_stuck(phase_error_fingerprints):
                    emit("log", msg="[PHASE_STUCK] INTEGRATE repeating same error — exiting phase early")
                    progress.log("INTEGRATE stuck on recurring error, advancing to VALIDATE")
                    state.advance()  # -> VALIDATE
                    messages = []
                    no_tool_rounds = 0
                    continue

                # Check if entry point passes
                if last_run:
                    cmd_str, cmd_result = last_run
                    if (entry_point in cmd_str
                            and cmd_result.get("exit_code") == 0
                            and "Traceback" not in cmd_result.get("stderr", "")):
                        _integrate_pass_count = getattr(state, '_integrate_pass_count', 0) + 1
                        state._integrate_pass_count = _integrate_pass_count
                        if _integrate_pass_count <= 1:
                            emit("log", msg="[INTEGRATE] Entry point passes! Routing to critic review...")
                            if "INTEGRATE" not in reflection_run_for_phase:
                                _run_phase_reflection(
                                    cfg, "INTEGRATE", task, manifest, progress, lessons, emit,
                                    failures="", success=True, tags=task_tags,
                                )
                                reflection_run_for_phase.add("INTEGRATE")
                            state.current = Phase.REVIEW
                            state.round_in_phase = 0
                            messages = []
                            no_tool_rounds = 0
                            continue
                        else:
                            emit("log", msg=f"[INTEGRATE] Entry point passes (x{_integrate_pass_count}). Tests already reviewed, advancing to VALIDATE.")
                            state.advance()  # -> VALIDATE
                            messages = []
                            no_tool_rounds = 0
                            continue

            # ── VALIDATE phase ──
            elif state.current == Phase.VALIDATE:
                executor.build_mode = False
                # Pre-validate: protect tsconfig from LLM overwrites during BUILD
                if lang and lang.family == "node":
                    if _protect_tsconfig(workspace, lang):
                        emit("log", msg="[Pre-validate] Restored tsconfig.json critical settings")
                    if _protect_package_json(workspace, lang):
                        emit("log", msg="[Pre-validate] Removed 'type: module' from package.json")
                    n = _strip_js_extensions_from_ts(workspace, manifest)
                    if n:
                        emit("log", msg=f"[Pre-validate] Stripped .js extensions from {n} TS files")
                # Defense-in-depth: re-run all three inspection tiers. Cheap (<200ms)
                # and catches drift accumulated during BUILD/INTEGRATE — especially
                # cross-module rename/refactor bugs that the post-modular commissioning
                # gate couldn't see because they happened later.
                _run_inspector("materials", workspace, lang, plan, emit)
                wiring_violations = _run_inspector("wiring", workspace, lang, plan, emit)
                # Last-chance rescue: if the LLM ignored the entry_point_missing warning
                # (it often does, treating the CLI module as the "real" entry), auto-write
                # a tiny shim at the declared path. Zero behavior change if LLM got it right.
                for v in wiring_violations:
                    if v.rule == "entry_point_missing" and v.severity == "error":
                        _auto_shim_entry_point(workspace, lang, v.file, emit)
                        break
                _run_inspector("commissioning", workspace, lang, plan, emit)
                emit("log", msg=f"[Running validation pipeline on {workspace}]")
                results = run_validation(
                    workspace, entry_point,
                    expected_packages=modular_plan.dependencies if modular_plan else None,
                    lang=lang,
                )
                progress.validation = results_to_dict(results)

                # Smart graduation: if run+tests pass, downgrade import errors to warnings
                rd = results_to_dict(results)
                if rd.get("run") and rd.get("tests") is not False:
                    for r in results:
                        if r.name == "imports" and not r.passed:
                            r.severity = "warning"

                emit("validation", results=[
                    {"name": r.name, "passed": r.passed, "output": r.output[:100] if r.output else "OK"}
                    for r in results
                ])

                failures_text = format_failures(results)
                warnings_list = [r for r in results if not r.passed and r.severity == "warning"]
                if not failures_text:
                    # Hard errors are clear. Surface warnings so "all passed" isn't a lie.
                    if warnings_list:
                        emit("log", msg=f"[Validation passed with {len(warnings_list)} warning(s)]")
                        for w in warnings_list:
                            emit("log", msg=f"  [WARN] {w.name}: {(w.output or '')[:200]}")
                    else:
                        emit("log", msg="[All validations passed!]")
                    progress.log("Validation passed" + (
                        f" ({len(warnings_list)} warning(s))" if warnings_list else ""
                    ))
                    progress.phase = "COMPLETE"
                    if "VALIDATE" not in reflection_run_for_phase:
                        _run_phase_reflection(
                            cfg, "VALIDATE", task, manifest, progress, lessons, emit,
                            failures="", success=True, tags=task_tags,
                        )
                        reflection_run_for_phase.add("VALIDATE")
                    break
                else:
                    last_failures_summary = failures_text[:1500]
                    emit("log", msg=f"[Validation failed, retry {state.validate_retries + 1}/{state.max_validate_retries}]")
                    progress.log(f"Validation failed: {failures_text[:100]}")
                    if state.retreat_to_build():
                        # Auto-install missing npm deps before retrying
                        installed = _auto_install_missing_deps(workspace, failures_text, lang, emit)
                        executor.grant_rewrites(max(len(manifest.files), 5))
                        code_map = _get_code_map(failure_text=failures_text)
                        emit("log", msg=f"[Code map refreshed: tier {_code_map_builder.tier}, {_code_map_builder.last_tokens} tokens]")
                        build_prompt = build_build_prompt(
                            entry_point=entry_point,
                            manifest_summary=manifest.to_detailed(),
                            progress_context=progress.to_context(),
                            validation_failures=failures_text,
                            lessons_text=lessons_text,
                            code_map=code_map,
                            lang=lang,
                        )
                        messages = _build_messages(build_prompt, task)
                        phase_summary = _create_phase_summary("VALIDATE", manifest, results, progress.build_log)
                        messages.append({"role": "user", "content":
                            f"{phase_summary}\n\n{failures_text}\n\n"
                            "FIX MODE: the validation failures above are authoritative. "
                            "Do NOT run the entry point — the harness has disabled run_command for this turn. "
                            "Read the failures, then use edit_file (or write_file) to fix them. "
                            "run_command is re-enabled after your first edit lands."
                        })
                        no_tool_rounds = 0
                    else:
                        emit("log", msg="[Max validation retries, stopping run()]")
                        _run_phase_reflection(
                            cfg, "VALIDATE_FAILED", task, manifest, progress, lessons, emit,
                            failures=failures_text, success=False, tags=task_tags,
                        )
                        progress.phase = "COMPLETE"
                        break

            # PACKAGE removed from run() — handled by _write_package_files() after run/build completes

            progress.write()

    except EndpointUnreachable as e:
        emit("error", msg=f"[ABORT endpoint_down] {e}")
        progress.log(f"ABORT: endpoint unreachable — {e}")
    except KeyboardInterrupt as e:
        # SIGTERM or Ctrl+C: we want to salvage whatever rounds were spent so
        # the next build's memory-aware budgets see this partial run. The
        # summary block below does the actual writing, then we re-raise to
        # let the outer build() / cmd_auto bail out cleanly (otherwise a
        # SIGTERM'd build would just continue into iterate phase).
        emit("error", msg=f"[ABORT signal] {e}")
        progress.log(f"ABORT: interrupted — {e}")
        _interrupted_for_reraise = e
    else:
        _interrupted_for_reraise = None
    finally:
        # Always restore the prior SIGTERM handler so subsequent run() calls
        # (iterate / enhance) don't inherit our kill-to-raise behavior.
        try:
            if _orig_sigterm is not None:
                signal.signal(signal.SIGTERM, _orig_sigterm)
        except (ValueError, OSError):
            pass

    # ── Summary ──
    elapsed = time.time() - t_start
    status = "COMPLETE" if progress.phase == "COMPLETE" else "STOPPED"
    files_str = ", ".join(manifest.files.keys()) if manifest.files else "none"
    emit("complete", status=status, total_rounds=state.total_rounds,
         n_files=len(manifest.files), elapsed=elapsed,
         files=files_str, workspace=workspace)

    # Feed the phase-budget history used by the next build's compute_budgets.
    # Only record when we have non-trivial data; also capture whatever the
    # current (unadvanced) phase spent, so a late-abort is visible too.
    try:
        tags = list(infer_task_tags(task))
        n_files_now = len(manifest.files)
        final_rounds = dict(state.phase_rounds_used)
        if state.round_in_phase > 0:
            final_rounds[state.current] = (
                final_rounds.get(state.current, 0) + state.round_in_phase
            )
        for phase, rounds in final_rounds.items():
            if rounds > 0:
                record_phase_outcome(phase.value, rounds, n_files_now, tags)
    except Exception:
        pass

    progress.write()
    build_log.close()

    # Signal propagation: after the summary block wrote the partial-build
    # phase rows, re-raise so outer build()/iterate() loops exit too.
    if _interrupted_for_reraise is not None:
        raise _interrupted_for_reraise


def _write_package_files(workspace: str, manifest: FileManifest, task: str, plan: dict | None = None, lang=None):
    """Write README.md and requirements.txt/package.json deterministically from manifest data."""
    deps = plan.get("dependencies", []) if plan else []
    default_entry = lang.entry_point if lang else "main.py"
    entry = plan.get("entry_point", default_entry) if plan else default_entry
    files = sorted(manifest.files.keys())
    is_ts = lang and lang.family == "node"
    is_html = lang and lang.family == "static"

    # requirements.txt / package.json (skip for static sites)
    if deps and not is_html:
        if is_ts:
            # For TS: update package.json if it exists, else create
            pkg_json_path = os.path.join(workspace, "package.json")
            if os.path.exists(pkg_json_path):
                with open(pkg_json_path) as f:
                    pkg = json.load(f)
            else:
                pkg = {"name": "project", "version": "1.0.0", "private": True}
            pkg.setdefault("dependencies", {})
            for dep in deps:
                if dep not in pkg["dependencies"]:
                    pkg["dependencies"][dep] = "*"
            with open(pkg_json_path, "w") as f:
                json.dump(pkg, f, indent=2)
        else:
            req_path = os.path.join(workspace, "requirements.txt")
            with open(req_path, "w") as f:
                for dep in deps:
                    f.write(dep + "\n")

    # README.md
    readme_path = os.path.join(workspace, "README.md")
    lines = [f"# {task.split('.')[0].strip()[:80]}", ""]
    lines.append(task)
    lines.append("")
    lines.append("## Setup")
    lines.append("```bash")
    if is_html:
        lines.append("# Open index.html in a browser")
        lines.append("# Or serve locally:")
        lines.append("python3 -m http.server 8080")
    elif deps:
        if is_ts:
            lines.append("npm install")
        else:
            lines.append("pip install -r requirements.txt")
    if is_html:
        lines.append("node test.js  # Run tests")
    else:
        run_cmd = lang.run_cmd if lang else "python3"
        lines.append(f"{run_cmd} {entry} --test  # Run self-tests")
    lines.append("```")
    lines.append("")
    lines.append("## Files")
    for f_name in files:
        info = manifest.files.get(f_name, {})
        purpose = info.get("purpose", "")
        lines.append(f"- `{f_name}` — {purpose}" if purpose else f"- `{f_name}`")
    lines.append("")
    with open(readme_path, "w") as f:
        f.write("\n".join(lines))


def _run_phase_reflection(cfg: Config, phase_name: str, task: str, manifest: FileManifest,
                          progress: Progress, lessons: list, emit, *,
                          failures: str = "", success: bool = True,
                          tags: list[str] | None = None):
    """Extract lessons from a single phase. On failure, biases toward anti-patterns.

    Gate: only runs if there are failures OR significant build_log activity (>5 entries).
    """
    if success and not failures and len(progress.build_log) <= 5:
        return
    emit("log", msg=f"[REFLECTION/{phase_name}] Extracting lessons (success={success})...")
    bias = ""
    if not success or failures:
        bias = (
            "\n\nNote: this phase had FAILURES. Most lessons should be ANTI patterns "
            "(use the `ANTI | TRIGGER | WHY` format). Capture concrete dead ends to avoid next time."
        )
    user_content = (
        f"Phase: {phase_name}\nTask: {task}\n"
        f"Files: {', '.join(list(manifest.files.keys())[:30])}\n"
        f"Build log (recent):\n" + "\n".join(progress.build_log[-20:])
    )
    if failures:
        user_content += f"\n\nFailure summary:\n{failures[:1500]}"
    reflection_msgs = [
        {"role": "system", "content": REFLECTION_PROMPT + bias},
        {"role": "user", "content": user_content},
    ]
    try:
        ref_msg = chat(cfg, reflection_msgs, tools=[], emit=emit)
    except Exception as e:
        emit("log", msg=f"[REFLECTION/{phase_name}] LLM call failed: {e}")
        return
    ref_text = (ref_msg.get("content") or "").strip()
    if ref_text and ref_text.upper() != "NONE":
        new_lessons = parse_reflection(ref_text, tags=tags)
        for lesson in new_lessons:
            save_lesson(lesson)
            emit("lesson", type=lesson.type, trigger=lesson.trigger[:60], fix=lesson.fix[:60])
        if lessons:
            boost_confidence(lessons, [l.trigger for l in lessons])


def _run_reflection(cfg: Config, task: str, manifest: FileManifest,
                    progress: Progress, lessons: list, emit):
    """Backward-compat wrapper — overall build reflection at end of run()."""
    _run_phase_reflection(
        cfg, "BUILD_OVERALL", task, manifest, progress, lessons, emit,
        success=(progress.phase == "COMPLETE"),
        tags=list(infer_task_tags(task)),
    )


# ── resume() — resume a build from last checkpoint ───────────────────────────

def resume(workspace: str, cfg: Config, emitter: EventEmitter | None = None):
    """Resume a build from its last checkpoint."""
    if emitter is None:
        emitter = EventEmitter()
        emitter.on(default_print_handler)
    emit = emitter.emit
    build_log = BuildLogger(workspace)
    emitter.on(build_log.handler)

    checkpoint = _load_checkpoint(workspace)
    if not checkpoint:
        emit("error", msg=f"No checkpoint found in {workspace}")
        build_log.close()
        return

    if not cfg.model:
        cfg.model = detect_model(cfg)

    task = checkpoint.get("task", "")
    phase_name = checkpoint.get("phase", "build")
    architecture_text = checkpoint.get("architecture", "")

    # Detect language from task/workspace
    lang = detect_language(task, workspace)
    entry_point = checkpoint.get("entry_point", lang.entry_point)

    emit("info", msg=f"Resuming: {workspace}")
    emit("info", msg=f"Task: {task[:60]}")
    emit("info", msg=f"Language: {lang.name}")
    emit("info", msg=f"From phase: {phase_name} (round {checkpoint.get('total_rounds', '?')})")
    emit("separator", char="═", width=60)

    # Rebuild state from checkpoint
    plan, manifest = _load_workspace(workspace, lang=lang)
    error_tracker = ErrorTracker(manifest)
    executor = ToolExecutor(workspace, manifest, progress_fn=lambda: "",
                            context_budget=cfg.max_context_tokens)
    executor.build_mode = True
    executor.grant_rewrites(max(len(manifest.files), 10))
    progress = Progress(task=task, workspace=workspace)

    # Map phase name to Phase enum
    phase_map = {p.value: p for p in Phase}
    resume_phase = phase_map.get(phase_name, Phase.BUILD)

    # If resuming from SCAFFOLD or earlier, re-enter BUILD
    if resume_phase in (Phase.PLAN, Phase.DEPS, Phase.SCAFFOLD, Phase.REVIEW):
        resume_phase = Phase.BUILD

    state = PhaseState()
    state.current = resume_phase
    state.total_rounds = checkpoint.get("total_rounds", 0)
    state.validate_retries = checkpoint.get("validate_retries", 0)

    # Load lessons
    lessons = recall(task)
    lessons_text = format_for_prompt(lessons)

    # Run validation to find current issues
    from .validate import run_validation, format_failures, results_to_dict
    results = run_validation(workspace, entry_point, lang=lang)
    failures_text = format_failures(results)
    emit("validation", results=[
        {"name": r.name, "passed": r.passed, "output": r.output[:100] if r.output else "OK"}
        for r in results
    ])

    if not failures_text:
        emit("log", msg="[All validations pass, advancing to PACKAGE]")
        state.current = Phase.PACKAGE

    # Build initial messages for the resumed phase
    if state.current == Phase.BUILD:
        build_prompt = build_build_prompt(
            entry_point=entry_point,
            manifest_summary=manifest.to_detailed(),
            progress_context=progress.to_context(),
            validation_failures=failures_text,
            lessons_text=lessons_text,
            lang=lang,
        )
        messages = _build_messages(build_prompt, task)
        messages.append({"role": "user", "content":
            f"Resuming build. Current validation state:\n{failures_text or 'All checks pass.'}\n\n"
            f"Fix any remaining issues, then run `{lang.run_cmd} {entry_point} --test`."
        })
    elif state.current == Phase.PACKAGE:
        # PACKAGE is now deterministic — just write files and done
        _write_package_files(workspace, manifest, task, plan, lang=lang)
        emit("log", msg="[PACKAGE] README.md and requirements.txt written")
        return
    else:
        emit("error", msg=f"Cannot resume from phase {state.current.value}")
        return

    no_tool_rounds = 0
    t_start = time.time()
    max_rounds = 30

    while no_tool_rounds < 3 and state.total_rounds < checkpoint.get("total_rounds", 0) + max_rounds:
        state.total_rounds += 1
        state.round_in_phase += 1

        emit("phase", label=f"RESUME/{state.current.value.upper()} (R{state.round_in_phase})",
             total_rounds=state.total_rounds, n_files=len(manifest.files),
             phase="resume", round=state.round_in_phase, budget=max_rounds)

        messages = trim_context(messages, max_tokens=cfg.max_context_tokens)
        msg = chat(cfg, messages, emit=emit)

        if not msg.get("tool_calls"):
            messages.append(msg)
            no_tool_rounds += 1
            if state.current == Phase.PACKAGE:
                break
            messages.append({"role": "user", "content":
                f"Run `{lang.run_cmd} {entry_point} --test` to verify. Use run_command."
            })
            continue

        no_tool_rounds = 0
        n_exec, _, error_intervention, last_run = process_tool_calls(
            msg, messages, executor, error_tracker, None, progress, emit=emit,
        )

        if error_intervention:
            messages.append({"role": "user", "content": error_intervention})

        if last_run and state.current == Phase.BUILD:
            cmd_str, cmd_result = last_run
            if (entry_point in cmd_str
                    and cmd_result.get("exit_code") == 0
                    and "Traceback" not in cmd_result.get("stderr", "")):
                emit("log", msg="[Entry point runs clean, validating]")
                results = run_validation(workspace, entry_point, lang=lang)
                emit("validation", results=[
                    {"name": r.name, "passed": r.passed, "output": r.output[:100] if r.output else "OK"}
                    for r in results
                ])
                if not format_failures(results):
                    emit("log", msg="[All validations pass]")
                    break

    _save_checkpoint(workspace, state, manifest, plan, architecture_text, entry_point, task)
    elapsed = time.time() - t_start
    emit("complete", status="RESUME DONE", total_rounds=state.total_rounds,
         n_files=len(manifest.files), elapsed=elapsed,
         files=", ".join(manifest.files.keys()), workspace=workspace)
    build_log.close()


# ── iterate() — re-run BUILD→VALIDATE on existing workspace ──────────────────

def iterate(workspace: str, cfg: Config, instruction: str = "",
            emitter: EventEmitter | None = None,
            max_rounds: int | None = None,
            read_budget_override: int | None = None):
    """Re-run BUILD→VALIDATE on an existing workspace."""
    from .quality import build_iterate_prompt
    from .codemap import CodeMapBuilder

    if emitter is None:
        emitter = EventEmitter()
        emitter.on(default_print_handler)
    emit = emitter.emit
    build_log = BuildLogger(workspace)
    emitter.on(build_log.handler)

    if not cfg.model:
        cfg.model = detect_model(cfg)

    # Detect language from workspace
    lang = detect_language(instruction or "", workspace)

    plan, manifest = _load_workspace(workspace, lang=lang)
    entry_point = plan.get("entry_point", lang.entry_point) if plan else lang.entry_point

    # Modular scoped iterate: detect affected modules and iterate on them
    if plan and plan.get("modular"):
        modular_plan = ModularPlan.from_dict(plan, lang=lang)

        # Run validation to get failure text for scoping
        results = run_validation(workspace, entry_point, lang=lang)
        failures_text = format_failures(results)

        affected = detect_affected_modules(instruction, failures_text, modular_plan, workspace)
        emit("info", msg=f"[ITERATE] Modular project — scoping to: {', '.join(affected)}")
        emit("separator", char="═", width=60)

        lessons = recall(instruction or "iterate")
        lessons_text = format_for_prompt(lessons)

        # Iterate on each affected module
        for mod_name in affected:
            _iterate_module(mod_name, modular_plan, workspace, cfg,
                            instruction, emit, lessons_text,
                            max_rounds=max_rounds or 20, lang=lang)

        # After module fixes, run full validation for integration
        emit("log", msg="[ITERATE] Running full validation after module fixes...")
        results = run_validation(workspace, entry_point, lang=lang)
        emit("validation", results=[
            {"name": r.name, "passed": r.passed, "output": r.output[:100] if r.output else "OK"}
            for r in results
        ])

        elapsed = 0  # Not tracked for modular iterate
        emit("complete", status="ITERATE DONE (modular)", total_rounds=0,
             n_files=len(manifest.files), elapsed=elapsed,
             files=", ".join(manifest.files.keys()), workspace=workspace)
        build_log.close()
        return

    # Standard flat iterate path
    error_tracker = ErrorTracker(manifest)
    executor = ToolExecutor(workspace, manifest, context_budget=cfg.max_context_tokens)
    executor.build_mode = True
    executor.config_writes_allowed = False  # iterate is post-scaffold by definition
    executor.scratch = Scratch(workspace)
    executor.phase_name = "ITERATE"
    executor.grant_rewrites(max(len(manifest.files), 10))
    progress = Progress(task=instruction or "iterate", workspace=workspace)

    # Pre-iterate safeguards: undo any corruption left by a prior run/iterate/manual edit.
    if lang and lang.family == "node":
        notes = []
        if _protect_tsconfig(workspace, lang):
            notes.append("tsconfig.json restored")
        if _protect_package_json(workspace, lang):
            notes.append("package.json 'type:module' stripped")
        n = _strip_js_extensions_from_ts(workspace, manifest)
        if n:
            notes.append(f".js stripped from {n} TS files")
        if notes:
            emit("log", msg=f"[Pre-iterate safeguards] {'; '.join(notes)}")

    emit("info", msg=f"Iterating on: {workspace}")
    emit("info", msg=f"Files: {len(manifest.files)} | Entry: {entry_point}")
    emit("separator", char="═", width=60)

    # Run validation to find current issues
    results = run_validation(workspace, entry_point, lang=lang)
    failures_text = format_failures(results)
    emit("validation", results=[
        {"name": r.name, "passed": r.passed, "output": r.output[:100] if r.output else "OK"}
        for r in results
    ])

    if not failures_text and not instruction:
        emit("log", msg="[All validations pass, nothing to iterate on]")
        return

    # Build budget-aware code map
    prompt_overhead = len(instruction or "") // 4 + 2000  # rough estimate for prompt chrome
    budget = max(cfg.max_context_tokens - prompt_overhead - 8000, 10000)  # reserve for conversation + output
    code_map_builder = CodeMapBuilder(workspace, budget_tokens=budget, lang=lang)
    code_map = code_map_builder.build(failure_text=failures_text)
    emit("log", msg=f"[Code map: tier {code_map_builder.tier}, {code_map_builder.last_tokens} est. tokens]")

    # Build iterate prompt
    iterate_prompt = build_iterate_prompt(
        entry_point=entry_point,
        manifest_summary=manifest.to_detailed(),
        validation_failures=failures_text,
        instruction=instruction,
        code_map=code_map,
        lang=lang,
    )
    messages = _build_messages(iterate_prompt, instruction or "Fix the current issues.")

    def _rebuild_messages(failures_override=None):
        """Rebuild messages from scratch with fresh code map + edit history."""
        fresh_failures = failures_override or failures_text
        fresh_map = code_map_builder.rebuild(
            changed_files=_edited_files,
            failure_text=fresh_failures,
        )
        emit("log", msg=f"[Code map refreshed: tier {code_map_builder.tier}, {code_map_builder.last_tokens} tokens]")
        fresh_prompt = build_iterate_prompt(
            entry_point=entry_point,
            manifest_summary=manifest.to_detailed(),
            validation_failures=fresh_failures,
            instruction=instruction,
            code_map=fresh_map,
            lang=lang,
        )
        msgs = _build_messages(fresh_prompt, instruction or "Fix the current issues.")
        # Inject edit history so model knows what was already tried
        if edit_log:
            history = "EDIT HISTORY (these changes were already applied):\n"
            history += "\n".join(f"  - {entry}" for entry in edit_log[-10:])
            msgs.append({"role": "user", "content": history})
        # Preserve last reasoning so model doesn't repeat failed approaches
        if last_reasoning:
            msgs.append({"role": "assistant", "content": last_reasoning})
        return msgs

    # Iterate tool set: edit tools + run_command + limited read_file
    ITERATE_TOOL_NAMES = {"edit_file", "write_file", "line_edit", "run_command", "read_file"}
    ITERATE_TOOLS = [t for t in TOOL_DEFS if t["function"]["name"] in ITERATE_TOOL_NAMES]
    EDIT_TOOLS = {"edit_file", "write_file", "line_edit", "write_test"}

    state = PhaseState()
    state.current = Phase.BUILD
    n_files = len(manifest.files)
    state.max_rounds[Phase.BUILD] = max_rounds or max(20, n_files * 2 + 10)
    no_tool_rounds = 0
    no_edit_rounds = 0
    _edited_files: set[str] = set()
    edit_log: list[str] = []  # Survives message rebuilds
    read_budget = read_budget_override or max(8, n_files + 3)
    last_reasoning: str = ""  # Preserve model reasoning across rebuilds
    read_count = 0
    t_start = time.time()

    while state.tick():
        _budget = state.max_rounds[Phase.BUILD]
        emit("phase", label=f"ITERATE (R{state.round_in_phase}/{_budget})",
             total_rounds=state.total_rounds, n_files=len(manifest.files),
             phase="iterate", round=state.round_in_phase, budget=_budget)

        messages = trim_context(messages, max_tokens=cfg.max_context_tokens)
        msg = chat(cfg, messages, tools=ITERATE_TOOLS, emit=emit)

        if not msg.get("tool_calls"):
            messages.append(msg)
            no_tool_rounds += 1
            if no_tool_rounds >= 3:
                break
            messages.append({"role": "user", "content":
                f"Run `{lang.run_cmd} {entry_point} --test` to verify. Use run_command."
            })
            continue

        no_tool_rounds = 0

        # Snapshot manifest versions so we can tell whether edits actually succeeded
        # (vs were rejected by _config_guard). "has_edit" must mean real change.
        _pre_versions = {p: info["version"] for p, info in manifest.files.items()}

        # Dynamic read budget: block read_file after budget exhausted
        current_allowed = set(ITERATE_TOOL_NAMES)
        if read_count >= read_budget:
            current_allowed.discard("read_file")

        executor.round_num = state.round_in_phase
        n_exec, _, error_intervention, last_run = process_tool_calls(
            msg, messages, executor, error_tracker, None, progress, emit=emit,
            allowed_tools=current_allowed,
        )

        # Per-round hygiene — same helper used by run()'s BUILD loop.
        _post_round_safeguards(workspace, lang, manifest, messages, msg, emit)

        # Compute has_edit from actual manifest deltas, not tool_calls intent.
        # Also populate _edited_files and edit_log from successful writes only.
        has_edit = False
        for p, info in manifest.files.items():
            prev = _pre_versions.get(p, 0)
            if info["version"] > prev:
                has_edit = True
                _edited_files.add(p)
                verb = "write" if prev == 0 else "edit"
                edit_log.append(f"R{state.round_in_phase}: {verb}_file({p}) -> v{info['version']}")

        # Track read_file usage
        for tc in msg.get("tool_calls", []):
            if tc["function"]["name"] == "read_file":
                read_count += 1
        if read_count == read_budget:
            emit("log", msg=f"[Read budget exhausted ({read_budget})]")

        # Capture model reasoning before any rebuild (Fix 2E)
        if msg.get("content"):
            last_reasoning = msg["content"][:500]

        if has_edit:
            no_edit_rounds = 0

            # Get fresh test output — either from model's run_command or by running validation
            test_output = ""
            if last_run:
                _, cmd_result = last_run
                test_output = cmd_result.get("stdout", "") + cmd_result.get("stderr", "")

            if not test_output:
                # Model edited but didn't run tests — run validation to get fresh failures
                fresh_results = run_validation(workspace, entry_point, lang=lang)
                fresh_failures = format_failures(fresh_results)
                test_output = fresh_failures or ""
                emit("log", msg="[Auto-validated after edit]")

            # Log test result summary into edit_log
            if test_output:
                # Extract a one-line summary of what passed/failed
                for line in test_output.splitlines():
                    if "passed" in line or "failed" in line or "PASSED" in line or "FAILED" in line:
                        edit_log.append(f"  Result: {line.strip()[:120]}")
                        break

            # Rebuild messages with fresh code map + fresh failures + edit history
            messages = _rebuild_messages(failures_override=test_output if test_output else None)
            _edited_files.clear()

            if test_output:
                messages.append({"role": "user", "content":
                    f"You just edited files. Here is the latest test output:\n```\n{test_output[:2000]}\n```\nFix any remaining failures."
                })
        else:
            no_edit_rounds += 1
            if no_edit_rounds >= 3:
                nudge = (
                    "You MUST call edit_file or write_file NOW. "
                    "The code map has all the source code. Pick the most impactful fix and apply it. "
                    "If you've already tried and failed, first call note_lesson('tried_failed', '<what broke>') "
                    "so you don't retry that approach — then try a DIFFERENT angle."
                )
                messages.append({"role": "user", "content": nudge})
                emit("log", msg=f"[Nudge: {no_edit_rounds} rounds without edits]")
                no_edit_rounds = 0

        if error_intervention:
            messages.append({"role": "user", "content": error_intervention})

        if last_run:
            cmd_str, cmd_result = last_run
            if (entry_point in cmd_str
                    and cmd_result.get("exit_code") == 0
                    and "Traceback" not in cmd_result.get("stderr", "")):
                # Entry point runs clean — verify full validation before exiting
                exit_results = run_validation(workspace, entry_point, lang=lang)
                exit_rd = results_to_dict(exit_results)
                if exit_rd.get("run") and exit_rd.get("tests") is not False:
                    emit("log", msg="[All validations pass — iterate complete]")
                    break
                else:
                    # Entry point ok but tests fail — keep going
                    exit_failures = format_failures(exit_results)
                    if exit_failures:
                        messages.append({"role": "user", "content":
                            f"Entry point runs but tests still fail:\n```\n{exit_failures[:2000]}\n```"
                        })

    # Final validation
    results = run_validation(workspace, entry_point, lang=lang)
    emit("validation", results=[
        {"name": r.name, "passed": r.passed, "output": r.output[:100] if r.output else "OK"}
        for r in results
    ])

    elapsed = time.time() - t_start
    emit("complete", status="ITERATE DONE", total_rounds=state.total_rounds,
         n_files=len(manifest.files), elapsed=elapsed,
         files=", ".join(manifest.files.keys()), workspace=workspace)
    build_log.close()


# ── build() — full pipeline: run() then auto-iterate until tests pass ─────────

def build(task: str, workspace: str, cfg: Config, max_iterations: int = 3,
          emitter: EventEmitter | None = None, parallel: bool = False):
    """Full pipeline: run() builds the project, then iterate() fixes until tests pass, then package."""
    if emitter is None:
        emitter = EventEmitter()
        emitter.on(default_print_handler)
    emit = emitter.emit

    # Phase 1: Initial build (PLAN → DEPS → SCAFFOLD → REVIEW → BUILD → VALIDATE)
    run(task, workspace, cfg, emitter=emitter, parallel=parallel)

    # Phase 2: Auto-iterate until clean
    lang = detect_language(task, workspace)
    plan, manifest = _load_workspace(workspace, lang=lang)
    entry_point = plan.get("entry_point", lang.entry_point) if plan else lang.entry_point
    lessons = load_lessons()
    progress = Progress(task=task, workspace=workspace)

    all_pass = False
    for i in range(max_iterations):
        results = run_validation(workspace, entry_point, lang=lang)
        failures = format_failures(results)
        if not failures:
            all_pass = True
            emit("log", msg="[AUTO] All validations pass!")
            break

        n_failures = len([r for r in results if not r.passed and r.severity == "error"])
        emit("separator", char="─", width=60)
        emit("log", msg=f"[AUTO] Iterate round {i + 1}/{max_iterations} — {n_failures} failure(s) remaining")
        iterate(workspace, cfg, emitter=emitter)

    if not all_pass:
        # Final check after last iteration
        results = run_validation(workspace, entry_point, lang=lang)
        failures = format_failures(results)
        all_pass = not failures
        if not all_pass:
            emit("log", msg=f"[AUTO] {max_iterations} iterations complete, some failures remain")

    # Phase 3: Package (README.md, requirements.txt / package.json) — always, even with failures
    emit("separator", char="─", width=60)
    emit("log", msg="[PACKAGE] Writing README.md and package files")
    # Reload manifest in case iterate() changed files
    plan, manifest = _load_workspace(workspace, lang=lang)
    _write_package_files(workspace, manifest, task, plan, lang=lang)
    emit("log", msg="[PACKAGE] Done")

    # Phase 4: Reflection — learn from this build
    _run_reflection(cfg, task, manifest, progress, lessons, emit)


def enhance(
    workspace: str,
    task: str,
    cfg: Config,
    emitter: EventEmitter | None = None,
):
    """Enhance an existing codebase: analyze → plan delta → build → validate."""
    if emitter is None:
        emitter = EventEmitter()
        emitter.on(default_print_handler)
    emit = emitter.emit

    workspace = os.path.abspath(workspace)
    if not os.path.isdir(workspace):
        emit("error", msg=f"Directory not found: {workspace}")
        return

    build_log = BuildLogger(workspace)
    emitter.on(build_log.handler)

    # Detect language from task/workspace
    lang = detect_language(task, workspace)

    emit("info", msg=f"Enhancing: {workspace}")
    emit("info", msg=f"Task: {task}")
    emit("info", msg=f"Language: {lang.name}")

    # Load existing codebase
    _, manifest = _load_workspace(workspace, lang=lang)
    n_files = len(manifest.files)
    emit("info", msg=f"Found {n_files} source files")

    if n_files == 0:
        emit("error", msg="No source files found in workspace")
        return

    # Set up tools and code map
    from .codemap import CodeMapBuilder
    progress = Progress(task=task, workspace=workspace)
    executor = ToolExecutor(workspace, manifest, progress_fn=progress.to_context,
                            context_budget=cfg.max_context_tokens)
    executor.build_mode = True
    _code_map_builder = CodeMapBuilder(workspace, budget_tokens=cfg.max_context_tokens // 2, lang=lang)
    error_tracker = ErrorTracker(manifest)

    def _get_code_map() -> str:
        return _code_map_builder.build()

    def _build_messages(system_prompt: str, user_task: str) -> list[dict]:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_task},
        ]

    # ── Phase 1: ANALYZE (read-only) ──
    emit("separator", char="─", width=60)
    emit("info", msg="[ANALYZE] Reading existing codebase...")
    code_map = _get_code_map()
    emit("log", msg=f"[Code map: {_code_map_builder.last_tokens} tokens]")

    analyze_prompt = build_analyze_prompt(code_map=code_map, lang=lang)
    messages = _build_messages(analyze_prompt, f"Analyze this codebase. I want to: {task}")

    read_only_tools = [t for t in TOOL_DEFS if t["function"]["name"] in
                       ("read_file", "list_files", "search_files", "run_command")]

    analysis_text = ""
    for round_num in range(5):
        msg = chat(cfg, messages, tools=read_only_tools, emit=emit)
        content = (msg.get("content") or "").strip()

        if msg.get("tool_calls"):
            process_tool_calls(msg, messages, executor, error_tracker, None, progress, emit=emit)
        else:
            analysis_text = content
            messages.append(msg)
            break
        messages.append(msg)

    if not analysis_text:
        # Force a summary if LLM only used tools
        messages.append({"role": "user", "content": "Now output your structured analysis of the codebase."})
        msg = chat(cfg, messages, tools=[], emit=emit)
        analysis_text = (msg.get("content") or "").strip()

    emit("log", msg=f"[ANALYZE] Analysis complete ({len(analysis_text)} chars)")

    # ── Phase 2: DELTA PLAN ──
    emit("separator", char="─", width=60)
    emit("info", msg="[PLAN] Planning minimal changes...")
    code_map = _get_code_map()  # Refresh
    delta_prompt = build_delta_plan_prompt(analysis=analysis_text, task=task, code_map=code_map, lang=lang)
    messages = _build_messages(delta_prompt, task)
    msg = chat(cfg, messages, tools=[], emit=emit)
    delta_text = (msg.get("content") or "").strip()

    # Try to parse as JSON
    delta_plan = extract_json(delta_text)
    if not delta_plan or not isinstance(delta_plan, dict):
        emit("log", msg="[PLAN] Could not parse delta plan as JSON, using text")
        delta_plan_str = delta_text
    else:
        n_create = len(delta_plan.get("create", []))
        n_modify = len(delta_plan.get("modify", []))
        new_deps = delta_plan.get("dependencies", [])
        emit("log", msg=f"[PLAN] Delta: {n_create} new files, {n_modify} modifications, {len(new_deps)} new deps")
        delta_plan_str = json.dumps(delta_plan, indent=2)

        # Save delta plan
        with open(os.path.join(workspace, ".cadillac_delta.json"), "w") as f:
            json.dump(delta_plan, f, indent=2)

    # ── Phase 2.5: Install new dependencies ──
    if delta_plan and isinstance(delta_plan, dict):
        new_deps = delta_plan.get("dependencies", [])
        if new_deps:
            emit("log", msg=f"[DEPS] Installing: {', '.join(new_deps)}")
            if lang.family == "static":
                emit("log", msg=f"[{lang.name} project — skipping deps]")
            elif lang.family == "node":
                executor.run_command(f"npm install {' '.join(new_deps)}")
            else:
                for dep in new_deps:
                    executor.run_command(_pip_install_cmd(dep, workspace))

    # ── Phase 3: BUILD (create + modify) ──
    emit("separator", char="─", width=60)
    emit("info", msg="[BUILD] Implementing changes...")

    # Detect entry point from existing project
    if lang.family == "static":
        candidates = ["index.html", "home.html", "main.html"]
    elif lang.family == "node":
        candidates = ["src/index.ts", "index.ts", "src/index.js", "index.js", "app.ts", "server.ts",
                       "src/main.tsx", "src/main.ts"]
    elif lang.family == "compiled":
        candidates = ["main.go", "src/main.rs", "main.c", "main.cpp", "src/Main.java"]
    else:
        candidates = ["main.py", "app.py", "cli.py", "server.py", "run.py"]
    entry_point = lang.entry_point
    for candidate in candidates:
        if os.path.exists(os.path.join(workspace, candidate)):
            entry_point = candidate
            break

    code_map = _get_code_map()
    build_prompt = build_enhance_build_prompt(
        delta_plan=delta_plan_str,
        analysis=analysis_text[:3000],
        entry_point=entry_point,
        code_map=code_map,
        lang=lang,
    )
    messages = _build_messages(build_prompt, f"Implement the delta plan for: {task}")
    test_cmd_str = " ".join(lang.test_cmd) if lang else "python3 -m pytest -x --tb=short -q"
    messages.append({"role": "user", "content":
        f"Start implementing. Create new files first, then modify existing ones. "
        f"After changes, run: {test_cmd_str}"
    })

    max_build_rounds = 40
    _code_map_stale = False
    for round_num in range(max_build_rounds):
        messages = trim_context(messages, max_tokens=cfg.max_context_tokens)

        # Refresh code map periodically
        if round_num > 0 and round_num % 5 == 0 and _code_map_stale:
            code_map = _get_code_map()
            emit("log", msg=f"[Code map refreshed: {_code_map_builder.last_tokens} tokens]")
            _code_map_stale = False

        msg = chat(cfg, messages, tools=TOOL_DEFS, emit=emit)
        content = (msg.get("content") or "").strip()

        if msg.get("tool_calls"):
            n_exec, _, error_intervention, last_run = process_tool_calls(
                msg, messages, executor, error_tracker, None, progress, emit=emit,
            )
            if n_exec > 0:
                _code_map_stale = True
            if error_intervention:
                messages.append({"role": "user", "content": error_intervention})

            # Check if tests pass
            if last_run:
                _, cmd_result = last_run
                stdout = cmd_result.get("stdout", "")
                exit_code = cmd_result.get("exit_code", 1)
                if exit_code == 0 and "passed" in stdout and "failed" not in stdout.lower():
                    emit("log", msg="[BUILD] Tests pass!")
                    break
        else:
            messages.append(msg)
            if not content or "done" in content.lower() or "complete" in content.lower():
                break

    # ── Phase 4: VALIDATE ──
    emit("separator", char="─", width=60)
    emit("info", msg="[VALIDATE] Running validation...")
    results = run_validation(workspace, entry_point, lang=lang)
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        emit("log", msg=f"  [{status}] {r.name}")

    failures = format_failures(results)
    if not failures:
        emit("info", msg="[ENHANCE] All validations pass!")
    else:
        emit("log", msg=f"[ENHANCE] {len([r for r in results if not r.passed])} validation(s) failed")

    emit("complete", status="ENHANCE DONE",
         total_rounds=0, n_files=len(manifest.files),
         elapsed=0, files=", ".join(sorted(manifest.files.keys())),
         workspace=workspace)

    if all_pass:
        emit("log", msg="[AUTO] Build complete — all tests pass!")

    build_log.close()


# ── debug() — focused single-issue debug ─────────────────────────────────────

def debug(workspace: str, cfg: Config, target: str = "", emitter: EventEmitter | None = None):
    """Run validation, then focus on fixing a specific issue."""
    from .quality import build_debug_prompt

    if emitter is None:
        emitter = EventEmitter()
        emitter.on(default_print_handler)
    emit = emitter.emit
    build_log = BuildLogger(workspace)
    emitter.on(build_log.handler)

    if not cfg.model:
        cfg.model = detect_model(cfg)

    # Detect language
    lang = detect_language(target or "", workspace)

    plan, manifest = _load_workspace(workspace, lang=lang)
    entry_point = plan.get("entry_point", lang.entry_point) if plan else lang.entry_point
    error_tracker = ErrorTracker(manifest)
    executor = ToolExecutor(workspace, manifest, context_budget=cfg.max_context_tokens)
    executor.build_mode = True
    executor.grant_rewrites(max(len(manifest.files), 10))
    progress = Progress(task=f"debug: {target}", workspace=workspace)

    emit("info", msg=f"Debugging: {workspace}")
    emit("separator", char="═", width=60)

    # Run validation
    results = run_validation(workspace, entry_point, lang=lang)
    failures = [r for r in results if not r.passed]
    emit("validation", results=[
        {"name": r.name, "passed": r.passed, "output": r.output[:100] if r.output else "OK"}
        for r in results
    ])

    if not failures:
        emit("log", msg="[All validations pass, nothing to debug]")
        return

    # Pick target failure
    if target:
        target_failures = [r for r in failures if target.lower() in r.name.lower() or target.lower() in (r.output or "").lower()]
        target_text = format_failures(target_failures) if target_failures else format_failures(failures[:1])
    else:
        target_text = format_failures(failures[:1])

    debug_prompt = build_debug_prompt(
        entry_point=entry_point,
        manifest_summary=manifest.to_detailed(),
        target_failure=target_text,
        lang=lang,
    )
    messages = _build_messages(debug_prompt, f"Fix this issue: {target or 'first failure'}")

    state = PhaseState()
    state.current = Phase.BUILD
    state.max_rounds[Phase.BUILD] = 10
    t_start = time.time()

    while state.tick():
        emit("phase", label=f"DEBUG (R{state.round_in_phase}/10)",
             total_rounds=state.total_rounds, n_files=len(manifest.files),
             phase="debug", round=state.round_in_phase, budget=10)

        messages = trim_context(messages, max_tokens=cfg.max_context_tokens)
        msg = chat(cfg, messages, emit=emit)

        if not msg.get("tool_calls"):
            messages.append(msg)
            break

        n_exec, _, _, last_run = process_tool_calls(
            msg, messages, executor, error_tracker, None, progress, emit=emit,
        )

        if last_run:
            cmd_str, cmd_result = last_run
            if (entry_point in cmd_str
                    and cmd_result.get("exit_code") == 0
                    and "Traceback" not in cmd_result.get("stderr", "")):
                emit("log", msg="[Issue fixed, entry point runs clean]")
                break

    # Final validation
    results = run_validation(workspace, entry_point, lang=lang)
    emit("validation", results=[
        {"name": r.name, "passed": r.passed, "output": r.output[:100] if r.output else "OK"}
        for r in results
    ])

    elapsed = time.time() - t_start
    emit("complete", status="DEBUG DONE", total_rounds=state.total_rounds,
         n_files=len(manifest.files), elapsed=elapsed,
         files=", ".join(manifest.files.keys()), workspace=workspace)
    build_log.close()
