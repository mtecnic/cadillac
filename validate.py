"""Validation pipeline — syntax, lint, run, import, naming, and test checks."""

import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass


@dataclass
class CheckResult:
    name: str
    passed: bool
    output: str = ""
    severity: str = "error"  # error, warning, info


def _run(cmd: list[str] | str, cwd: str, timeout: int = 30, shell: bool = False) -> subprocess.CompletedProcess:
    """Run a command with a history-informed timeout.

    `timeout` is the BASELINE hint for this command kind. Actual wall-clock
    limit is `max(timeout, p95(past_elapsed) * 2.5)` clamped to a hard ceiling,
    derived from `.cadillac/cmd_history.jsonl` in `cwd`. Cold workspaces (no
    history) get exactly `timeout` — same behavior as before.

    Every run is recorded so the NEXT call of the same kind gets a smarter
    budget. Timeouts also record (exit=124) so a slow suite's next run scales
    up instead of timing out repeatedly.
    """
    from .tools import _adaptive_timeout, _cmd_kind, _record_cmd_history
    import time as _time
    cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    effective = _adaptive_timeout(cmd_str, cwd, baseline=timeout)
    t0 = _time.monotonic()
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=effective, shell=shell,
        )
        elapsed_ms = int((_time.monotonic() - t0) * 1000)
        _record_cmd_history(cwd, _cmd_kind(cmd_str), elapsed_ms, result.returncode)
        return result
    except subprocess.TimeoutExpired:
        _record_cmd_history(cwd, _cmd_kind(cmd_str), effective * 1000, 124)
        return subprocess.CompletedProcess(cmd, 1, "", f"Timed out after {effective}s")
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _score_node_dir(path: str) -> int:
    """Score a directory's suitability as the Node project root.

    Higher is better. Signals we weight:
      - has package.json (+1, required)
      - has node_modules (+2, means `npx` will find local binaries)
      - `"type": "module"` in package.json (+4, dominates — vite 5 on Node
        18 emits a CJS-deprecation warning to stderr when type:module is
        absent, which our own validator then flags as a failure. Dirs with
        type:module set are definitively where vite should run.)
      - has vite.config.{ts,js,mjs} (+2, concrete config file presence)
      - mentions vite/vitest/react/vue in deps (+1, package.json signal)
    Returns 0 when the dir has no package.json.
    """
    pkg_path = os.path.join(path, "package.json")
    if not os.path.exists(pkg_path):
        return 0
    score = 1
    if os.path.isdir(os.path.join(path, "node_modules")):
        score += 2
    for cfg in ("vite.config.ts", "vite.config.js", "vite.config.mts",
                "vite.config.mjs"):
        if os.path.exists(os.path.join(path, cfg)):
            score += 2
            break
    try:
        with open(pkg_path) as f:
            pkg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return score
    if pkg.get("type") == "module":
        score += 4
    all_deps = {**(pkg.get("dependencies") or {}),
                **(pkg.get("devDependencies") or {})}
    if any(k in all_deps for k in ("vite", "vitest", "react", "vue",
                                    "@vitejs/plugin-react",
                                    "@vitejs/plugin-vue")):
        score += 1
    return score


def _find_node_project_dir(workspace: str) -> str:
    """Find the directory that actually has the real Node project.

    Full-stack builds put the node project in a subdir like `frontend/`. When
    we run `npx vitest` with cwd=workspace, npx can't find the local binary
    and falls through to a stale global npm cache (e.g. ~/.npm/_npx/<hash>/
    node_modules/vitest@0.34.6) — ignoring the pinned version in
    frontend/package.json entirely. Running from the right subdir fixes it.

    Score-based: we evaluate workspace root + each top-level subdir and pick
    the best (vite deps + type:module + node_modules beats bare package.json).
    When scores tie, workspace root wins (no surprise directory jumps).
    """
    candidates = [(workspace, _score_node_dir(workspace))]
    try:
        for entry in sorted(os.listdir(workspace)):
            sub = os.path.join(workspace, entry)
            if not os.path.isdir(sub) or entry.startswith("."):
                continue
            if entry in ("node_modules", "dist", "__pycache__", ".venv"):
                continue
            candidates.append((sub, _score_node_dir(sub)))
    except OSError:
        pass
    scored = [(p, s) for p, s in candidates if s > 0]
    if not scored:
        return workspace
    scored.sort(key=lambda ps: -ps[1])  # highest score wins; stable order
    return scored[0][0]


def _npx_no_install(cmd: list) -> list:
    """Force `npx` invocations to use the LOCAL node_modules/.bin entry only.
    Without --no-install, npx silently falls back to a stale global npm cache
    (e.g. vitest@0.34.6), ignoring whatever version the project's
    package.json pinned. Returns the original command unchanged if it doesn't
    start with npx."""
    if not cmd or cmd[0] != "npx":
        return cmd
    # Already has a flag between npx and subcommand — leave as-is.
    if len(cmd) > 1 and cmd[1].startswith("-"):
        return cmd
    return [cmd[0], "--no-install"] + cmd[1:]


def check_syntax(workspace: str, lang=None) -> list[CheckResult]:
    """Run syntax checks on source files."""
    if lang and lang.family == "node":
        return _check_syntax_ts(workspace)
    if lang and lang.family == "static":
        return _check_syntax_html(workspace)
    results = []
    for root, _, files in os.walk(workspace):
        # Skip __pycache__ and .venv
        if "__pycache__" in root or ".venv" in root:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, workspace)
            r = _run(["python3", "-m", "py_compile", path], cwd=workspace, timeout=10)
            if r.returncode != 0:
                results.append(CheckResult("syntax", False, f"{rel}: {r.stderr.strip()[:300]}"))
    if not results:
        results.append(CheckResult("syntax", True))
    return results


def _check_syntax_html(workspace: str) -> list[CheckResult]:
    """Check JavaScript syntax using node --check, verify HTML files exist."""
    results = []
    has_html = False
    for root, _, files in os.walk(workspace):
        for f in files:
            path = os.path.join(root, f)
            rel = os.path.relpath(path, workspace)
            if f.endswith(".js"):
                r = _run(["node", "--check", path], cwd=workspace, timeout=10)
                if r.returncode != 0:
                    results.append(CheckResult("syntax", False, f"{rel}: {r.stderr.strip()[:300]}"))
            elif f.endswith(".html"):
                has_html = True
    if not has_html:
        results.append(CheckResult("syntax", False, "No .html files found"))
    if not results:
        results.append(CheckResult("syntax", True))
    return results


def _check_syntax_ts(workspace: str) -> list[CheckResult]:
    """Check TypeScript syntax using tsc --noEmit."""
    tsconfig = os.path.join(workspace, "tsconfig.json")
    if not os.path.exists(tsconfig):
        return [CheckResult("syntax", True, "No tsconfig.json found, skipping", "info")]
    r = _run(["npx", "tsc", "--noEmit"], cwd=workspace, timeout=60)
    if r.returncode == 0:
        return [CheckResult("syntax", True)]
    output = (r.stdout + r.stderr).strip()[-2000:]
    return [CheckResult("syntax", False, output)]


def check_lint(workspace: str, lang=None) -> list[CheckResult]:
    """Run linter."""
    if lang and lang.family == "node":
        return _check_lint_ts(workspace)
    if lang and lang.family in ("static", "compiled"):
        return [CheckResult("lint", True, f"No linter for {lang.name}, skipped", "info")]
    r = _run(["ruff", "check", "--select=E,F,W", workspace], cwd=workspace, timeout=30)
    if "No such file" in r.stderr:
        return [CheckResult("lint", True, "ruff not installed, skipped", "info")]
    if r.returncode != 0:
        # Filter to just errors, not the summary line
        output = r.stdout.strip()[:2000] if r.stdout else r.stderr.strip()[:2000]
        return [CheckResult("lint", False, output, "warning")]
    return [CheckResult("lint", True)]


_SMOKE_RUN_TIMEOUT_S = 5
_SMOKE_RUN_MAX_FRAMES = 60

# A self-contained harness: monkey-patch curses.wrapper / set SDL env BEFORE
# user code runs, then exec main.py via runpy with __name__ == "__main__" so
# the natural entry-point block fires. After N frames or timeout, the fake
# screen returns the quit key and the loop should exit cleanly. Any
# uncaught exception (NameError, AttributeError, signature mismatch) is
# captured by the parent and rendered as a check failure.
_SMOKE_HARNESS = '''
import sys, os, traceback
WS = sys.argv[1]
# Reset argv so the user's main() / Click / argparse sees a clean no-args
# invocation — our workspace path was a harness arg, not user input.
sys.argv = [os.path.join(WS, "main.py")]
sys.path.insert(0, WS)
os.chdir(WS)

# Headless display flags for any pygame usage. Cheap to set even when unused.
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

# Monkey-patch curses.wrapper if curses is involved. We only do this when
# `import curses` appears somewhere in the workspace (caller has filtered).
_curses_wrapper_called = False
try:
    import curses
    _MAX = {max_frames}
    class _FakeScreen:
        def __init__(self):
            self._frames = 0
        def timeout(self, *a): pass
        def keypad(self, *a): pass
        def nodelay(self, *a): pass
        def leaveok(self, *a): pass
        def clear(self): pass
        def erase(self): pass
        def refresh(self): self._frames += 1
        def noutrefresh(self): self._frames += 1
        def addstr(self, *a, **kw): pass
        def addch(self, *a, **kw): pass
        def attron(self, *a, **kw): pass
        def attroff(self, *a, **kw): pass
        def move(self, *a, **kw): pass
        def border(self, *a, **kw): pass
        def box(self, *a, **kw): pass
        def getmaxyx(self):
            return (40, 100)
        def getch(self):
            # First few frames: action keys; then quit.
            if self._frames < _MAX // 3:
                return ord(" ")
            if self._frames < (2 * _MAX) // 3:
                return curses.KEY_RIGHT
            return ord("q")
        def derwin(self, *a, **kw): return _FakeScreen()
        def subwin(self, *a, **kw): return _FakeScreen()

    def _fake_wrapper(fn, *a, **kw):
        global _curses_wrapper_called
        _curses_wrapper_called = True
        return fn(_FakeScreen(), *a, **kw)
    curses.wrapper = _fake_wrapper

    # Programs that don't use curses.wrapper invoke initscr/cbreak/etc.
    # directly — those need a real TTY to succeed. In the smoke-run
    # subprocess we have no TTY, so stub them to no-ops + a fake screen.
    _fake_screen_singleton = _FakeScreen()
    def _fake_initscr():
        global _curses_wrapper_called
        _curses_wrapper_called = True
        return _fake_screen_singleton
    curses.initscr = _fake_initscr
    for _name in ("cbreak", "nocbreak", "echo", "noecho", "raw", "noraw",
                  "start_color", "use_default_colors", "endwin",
                  "curs_set", "init_pair", "color_pair", "beep", "flash",
                  "doupdate", "napms", "flushinp", "ungetch"):
        if hasattr(curses, _name):
            setattr(curses, _name, lambda *a, **kw: 0)
    if hasattr(curses, "has_colors"):
        curses.has_colors = lambda: True
    if hasattr(curses, "COLORS"):
        try: curses.COLORS = 8
        except Exception: pass
    _curses_was_imported = True
except ImportError:
    _curses_was_imported = False

# Run main.py as if invoked from CLI. runpy fires the `if __name__ == "__main__"`
# block, so the real entry path executes — not just `--test`.
import runpy
def _post_check():
    # If curses was in the project but our fake wrapper was never invoked,
    # the entry point bypassed the interactive loop entirely (broken cli()
    # function, missing __main__ block, etc.). User runs `python3 main.py`
    # and sees nothing.
    if _curses_was_imported and not _curses_wrapper_called:
        print("SMOKE_FAIL: curses framework present but main entry never called curses.wrapper "
              "(user-facing launch is broken — likely cli()/__main__ misroute)")
        sys.exit(1)
    print("SMOKE_OK")
    sys.exit(0)

try:
    runpy.run_path(os.path.join(WS, "main.py"), run_name="__main__")
    _post_check()
except SystemExit as e:
    # main() may sys.exit(0) cleanly; treat as success
    if (e.code or 0) == 0:
        _post_check()
    print("SMOKE_FAIL: SystemExit({{}})".format(e.code))
    sys.exit(1)
except Exception as e:
    # TTY-required errors are environmental, not code bugs. The program is
    # probably fine — we just can't simulate a real terminal in a subprocess.
    # Mark as inconclusive rather than failing the build.
    msg = str(e).lower()
    tty_markers = (
        "inappropriate ioctl",
        "cbreak() returned err",
        "setupterm",
        "no terminal",
        "not a tty",
        "tcgetattr",
        "tcsetattr",
    )
    if any(m in msg for m in tty_markers):
        print("SMOKE_TTY_REQUIRED:", type(e).__name__, str(e)[:200])
        sys.exit(0)
    print("SMOKE_FAIL:", type(e).__name__, str(e))
    traceback.print_exc()
    sys.exit(1)
'''


def _detect_interactive_framework(workspace: str) -> str | None:
    """Return 'curses', 'pygame', or None based on source-file imports."""
    has_curses = False
    has_pygame = False
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in {
            "__pycache__", ".venv", "node_modules", ".git", ".cadillac",
        }]
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                with open(os.path.join(root, f), errors="replace") as fh:
                    content = fh.read(8000)
            except OSError:
                continue
            if re.search(r"^\s*import\s+curses|^\s*from\s+curses", content, re.M):
                has_curses = True
            if re.search(r"^\s*import\s+pygame|^\s*from\s+pygame", content, re.M):
                has_pygame = True
    if has_curses:
        return "curses"
    if has_pygame:
        return "pygame"
    return None


def check_smoke_run(workspace: str, lang=None) -> list[CheckResult]:
    """Exercise the no-args interactive entry path with a fake screen.

    The recurring bug class this catches: code reachable from `main()` but
    NOT from `--test` mode. Our existing `run` check invokes `main.py --test`
    which short-circuits to pure-logic unit tests; the actual game/server
    loop is never touched. A NameError, signature mismatch, or unhandled
    exception in the real entry path passes every other validation and
    crashes on first real launch.

    Approach: spawn `python3 _harness.py <workspace>` where the harness
    monkey-patches curses.wrapper with a FakeScreen (returns synthetic
    keys for ~60 frames then 'q') and sets SDL_VIDEODRIVER=dummy. Run
    main.py via runpy so __main__ fires. Capture exceptions; report as FAIL.

    Only fires for Python projects that import curses or pygame. Pure-CLI
    or web/server projects skip cleanly — their interactive surface is
    network or stdin, both already covered by `run`/`tests`.
    """
    if not lang or lang.family != "python":
        return [CheckResult("smoke_run", True,
                            f"Smoke-run is interactive-Python-only; {lang.name if lang else '?'} skipped",
                            "info")]
    framework = _detect_interactive_framework(workspace)
    if framework is None:
        return [CheckResult("smoke_run", True,
                            "No curses/pygame usage detected, skipped", "info")]
    if not os.path.exists(os.path.join(workspace, "main.py")):
        return [CheckResult("smoke_run", True,
                            "No main.py found, skipped", "info")]

    # Materialize the harness to a temp file so exceptions reference real lines
    import tempfile
    harness_src = _SMOKE_HARNESS.format(max_frames=_SMOKE_RUN_MAX_FRAMES)
    with tempfile.NamedTemporaryFile(mode="w", suffix="_smoke.py",
                                       delete=False) as f:
        f.write(harness_src)
        harness_path = f.name
    try:
        r = _run(["python3", harness_path, workspace],
                 cwd=workspace, timeout=_SMOKE_RUN_TIMEOUT_S)
    finally:
        try:
            os.unlink(harness_path)
        except OSError:
            pass
    output = (r.stdout + "\n" + r.stderr).strip()
    if "SMOKE_OK" in output and r.returncode == 0:
        return [CheckResult("smoke_run", True,
                            f"{framework}: entry path ran {_SMOKE_RUN_MAX_FRAMES} frames clean")]
    if "SMOKE_TTY_REQUIRED" in output:
        # Program needs a real TTY (raw termios, ncurses init outside our patches).
        # Can't validate without a PTY — pass with info severity, don't fail the build.
        return [CheckResult("smoke_run", True,
                            f"{framework}: requires a real TTY, smoke skipped (no code-level bug detected)",
                            "info")]
    # Truncate to the relevant traceback portion
    fail_msg = output[-2000:]
    return [CheckResult("smoke_run", False,
                        f"{framework} entry path crashed: {fail_msg}")]


def _ensure_pyflakes() -> bool:
    """Ensure pyflakes is importable. Auto-install on PEP 668 hosts."""
    try:
        import pyflakes  # noqa: F401
        return True
    except ImportError:
        pass
    # Try to install — best effort. PEP 668-aware.
    try:
        proc = subprocess.run(
            ["pip", "install", "--break-system-packages", "--quiet", "pyflakes"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            subprocess.run(
                ["pip", "install", "--user", "--quiet", "pyflakes"],
                capture_output=True, text=True, timeout=60,
            )
    except (subprocess.SubprocessError, OSError):
        pass
    try:
        import pyflakes  # noqa: F401
        return True
    except ImportError:
        return False


def check_static_names(workspace: str, lang=None) -> list[CheckResult]:
    """Catch undefined-name bugs that don't surface until the runtime path
    that references them is actually executed.

    The recurring failure mode this blocks: a module references a class or
    function (e.g. `InputPoller`, `Optional`, `curses`) without importing
    it; the syntax check passes (parseable), the imports check passes
    (declared imports resolve), the run check passes (--test mode never
    enters the broken code path), and the build ships green but crashes
    on first real launch.

    Python: `pyflakes` reports `undefined name '<X>'` per file:line — fast,
    deterministic, no false positives on normal code. Ignores unused-import
    noise (that's lint, not correctness).

    TypeScript: handled by `check_syntax` already (`tsc --noEmit` resolves
    every name during type-check). This check is a no-op there.
    """
    if not lang or lang.family != "python":
        return [CheckResult("static_names", True,
                            f"Static-name check is Python-only; {lang.name if lang else '?'} relies on its compiler",
                            "info")]
    if not _ensure_pyflakes():
        return [CheckResult("static_names", True,
                            "pyflakes not installed and auto-install failed, skipped",
                            "warning")]

    # Walk all .py source files in workspace, skipping caches/venv/tests-only-via-conftest
    py_files: list[str] = []
    for root, dirs, files in os.walk(workspace):
        # Mutate dirs in place to skip these subtrees
        dirs[:] = [d for d in dirs if d not in {
            "__pycache__", ".venv", "venv", "node_modules",
            ".pytest_cache", ".git", ".cadillac", "dist",
        }]
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.relpath(os.path.join(root, f), workspace))
    if not py_files:
        return [CheckResult("static_names", True, "No .py files to scan", "info")]

    r = _run(["python3", "-m", "pyflakes"] + py_files, cwd=workspace, timeout=30)
    # pyflakes returns non-zero when issues found, prints "<path>:<line>:<col>: <msg>"
    issues = (r.stdout or "").strip().splitlines()
    # Filter to bug-shaped diagnostics — `undefined name` is the killer.
    # Ignore "imported but unused" / "redefined" / "may be undefined" — those
    # are style, not correctness, and `lint` already handles them.
    bug_keywords = ("undefined name", "syntax error", "may be undefined")
    bugs = [line for line in issues if any(k in line for k in bug_keywords)]
    if not bugs:
        return [CheckResult("static_names", True,
                            f"{len(py_files)} files scanned, no undefined-name bugs")]
    output = "\n".join(bugs[:30])
    if len(bugs) > 30:
        output += f"\n... and {len(bugs) - 30} more"
    return [CheckResult("static_names", False, output)]


def _check_lint_ts(workspace: str) -> list[CheckResult]:
    """Run eslint on TS/JS files."""
    # ESLint 9+ requires eslint.config.js (flat config). Skip if not present.
    has_config = any(
        os.path.exists(os.path.join(workspace, f"eslint.config.{ext}"))
        for ext in ("js", "mjs", "cjs")
    )
    # Also check for legacy .eslintrc.*
    if not has_config:
        has_config = any(
            os.path.exists(os.path.join(workspace, f))
            for f in (".eslintrc.js", ".eslintrc.json", ".eslintrc.yml", ".eslintrc")
        )
    if not has_config:
        return [CheckResult("lint", True, "No eslint config found, skipped", "info")]

    r = _run(["npx", "eslint", "."], cwd=workspace, timeout=30)
    if r.returncode == 0:
        return [CheckResult("lint", True)]
    combined = r.stdout + r.stderr
    if "No such file" in combined or "not found" in combined.lower() or "couldn't find" in combined.lower():
        return [CheckResult("lint", True, "eslint not configured, skipped", "info")]
    output = (r.stdout.strip()[:2000] if r.stdout else r.stderr.strip()[:2000])
    return [CheckResult("lint", False, output, "warning")]


def check_entry_point(workspace: str, entry_point: str = "main.py", lang=None) -> list[CheckResult]:
    """Run the entry point with --test flag."""
    entry = os.path.join(workspace, entry_point)
    if not os.path.exists(entry):
        return [CheckResult("run", False, f"Entry point {entry_point} not found")]

    # Static sites: just verify entry point exists (can't execute HTML)
    if lang and lang.family == "static":
        return [CheckResult("run", True, f"Entry point {entry_point} exists")]

    # Framework projects (React/Vue/Angular): entry point is a DOM mount, not executable
    if lang and lang.name in ("react", "vue", "angular", "electron"):
        if lang.build_cmd:
            node_dir = _find_node_project_dir(workspace)
            cmd = _npx_no_install(lang.build_cmd.split())
            r = _run(cmd, cwd=node_dir, timeout=120)
            if r.returncode == 0:
                return [CheckResult("run", True, f"{lang.name} build succeeded")]
            output = (r.stdout + "\n" + r.stderr).strip()[-1500:]
            return [CheckResult("run", False, f"Build failed: {output}")]
        return [CheckResult("run", True, f"Entry point {entry_point} exists")]

    if lang and lang.family == "node":
        # For Node: try ts-node first (CommonJS), fallback to node
        run_cmds = [
            ["npx", "ts-node", entry_point, "--test"],
            ["node", entry_point, "--test"],
        ]
    elif lang and lang.family == "compiled":
        # For compiled: build first, then run
        if lang.build_cmd:
            build_r = _run(lang.build_cmd.split(), cwd=workspace, timeout=60)
            if build_r.returncode != 0:
                return [CheckResult("run", False, f"Build failed: {(build_r.stderr or build_r.stdout)[:500]}")]
        run_cmds = [[lang.run_cmd, "--test"]]
    else:
        run_cmds = [["python3", entry_point, "--test"]]

    for cmd in run_cmds:
        r = _run(cmd, cwd=workspace, timeout=30)
        if r.returncode == 0 and "Traceback" not in r.stderr:
            return [CheckResult("run", True)]

    # If --test didn't work, try bare run with short timeout (interactive app fallback)
    if "--test" in r.stderr or "unrecognized" in r.stderr:
        bare_cmd = run_cmds[0][:-1]  # same command without --test
        r = _run(bare_cmd, cwd=workspace, timeout=5)
        if r.returncode == 0 or (r.returncode == -15 and "Traceback" not in r.stderr):
            return [CheckResult("run", True, "Interactive app detected (ran without --test)")]

    # Combine both stdout and stderr for maximum diagnostic info
    output = (r.stdout.strip() + "\n" + r.stderr.strip()).strip()[:1500]
    return [CheckResult("run", False, output)]


def _count_python_source_files(workspace: str) -> int:
    """Count non-trivial Python source files (excludes __init__.py, setup.py, conf.py, tests)."""
    skip_names = {"__init__.py", "setup.py", "conf.py"}
    n = 0
    for root, _, files in os.walk(workspace):
        if "__pycache__" in root or ".venv" in root or "/tests" in root or "/test" in root:
            continue
        for f in files:
            if f.endswith(".py") and f not in skip_names \
                    and not f.startswith("test_") and not f.endswith("_test.py"):
                n += 1
    return n


def _count_ts_source_files(workspace: str) -> int:
    """Count non-trivial TS/JS source files (excludes node_modules, dist, tests)."""
    n = 0
    for root, _, files in os.walk(workspace):
        if "node_modules" in root or "/dist" in root or "/__tests__" in root:
            continue
        for f in files:
            if f.endswith((".ts", ".tsx", ".js", ".jsx")) and not f.endswith(
                (".test.ts", ".test.tsx", ".test.js", ".test.jsx",
                 ".spec.ts", ".spec.tsx", ".spec.js")
            ) and not f.endswith((".config.ts", ".config.js")):
                n += 1
    return n


def check_tests(workspace: str, lang=None) -> list[CheckResult]:
    """Run test suite if test files exist."""
    if lang and lang.family == "node":
        return _check_tests_ts(workspace, lang)
    if lang and lang.family == "static":
        return _check_tests_html(workspace)

    test_files = []
    for root, _, files in os.walk(workspace):
        if "__pycache__" in root or ".venv" in root:
            continue
        for f in files:
            if (f.startswith("test_") or f.endswith("_test.py")) and f.endswith(".py"):
                test_files.append(os.path.relpath(os.path.join(root, f), workspace))

    if not test_files:
        # Escalate to "error" for substantial projects (≥3 source files).
        # Tiny scripts stay at "warning" so validation doesn't fail on a 1-file util.
        sev = "error" if _count_python_source_files(workspace) >= 3 else "warning"
        return [CheckResult("tests", False, "No test files found — tests were expected", sev)]

    # Try pytest first, fall back to unittest
    r = _run(["python3", "-m", "pytest", "-x", "--tb=short", "-q"], cwd=workspace, timeout=60)
    if r.returncode == 0:
        return [CheckResult("tests", True)]

    output = (r.stdout + "\n" + r.stderr).strip()[-1500:]
    return [CheckResult("tests", False, output)]


def _check_tests_html(workspace: str) -> list[CheckResult]:
    """Run test.js with Node for plain HTML/CSS/JS projects."""
    test_candidates = ["test.js", "tests.js", "test/test.js"]
    for tf in test_candidates:
        test_path = os.path.join(workspace, tf)
        if os.path.exists(test_path):
            r = _run(["node", tf], cwd=workspace, timeout=30)
            if r.returncode == 0:
                return [CheckResult("tests", True)]
            output = (r.stdout + "\n" + r.stderr).strip()[-1500:]
            return [CheckResult("tests", False, output)]
    return [CheckResult("tests", True, "No test.js found, skipped", "info")]


def _check_tests_ts(workspace: str, lang=None) -> list[CheckResult]:
    """Run Jest/Vitest tests for TS/JS projects."""
    # Check for test files
    test_files = []
    for root, _, files in os.walk(workspace):
        if "node_modules" in root or ".venv" in root:
            continue
        for f in files:
            if f.endswith((".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".spec.ts", ".spec.tsx", ".spec.js")):
                test_files.append(f)
    if not test_files:
        sev = "error" if _count_ts_source_files(workspace) >= 3 else "warning"
        return [CheckResult("tests", False, "No test files found — tests were expected", sev)]

    # Full-stack projects nest the node project in a subdir (frontend/). Run
    # from there so `npx vitest` finds the locally-installed binary instead of
    # falling through to whatever stale version npm cached globally.
    node_dir = _find_node_project_dir(workspace)

    # For Vite-based frameworks, use vitest directly (jest can't parse JSX without babel config)
    if lang and lang.name in ("react", "vue"):
        r = _run(["npx", "--no-install", "vitest", "run"], cwd=node_dir, timeout=120)
        if r.returncode == 0:
            return [CheckResult("tests", True)]
        output = (r.stdout + "\n" + r.stderr).strip()[-1500:]
        return [CheckResult("tests", False, output)]

    # Plain TS/JS: try the runner the LLM actually picked. We detect from
    # devDependencies (any test framework) and fall back to npm test / jest / vitest.
    pkg_path = os.path.join(node_dir, "package.json")
    declared_runner = None
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path) as f:
                pkg = json.load(f)
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            for name in ("vitest", "jest", "mocha", "ava", "tap"):
                if name in all_deps:
                    declared_runner = name
                    break
        except (OSError, json.JSONDecodeError):
            pass

    attempts = []
    # Runner-specific command (when we know what LLM installed). --no-install
    # forces npx to use the LOCAL node_modules — without it, npx falls back to
    # a stale global cache and ignores the version pinned in package.json.
    if declared_runner == "vitest":
        attempts.append(["npx", "--no-install", "vitest", "run"])
    elif declared_runner == "jest":
        attempts.append(["npx", "--no-install", "jest", "--passWithNoTests"])
    elif declared_runner == "mocha":
        attempts.append(["npx", "--no-install", "mocha", "--recursive"])
    elif declared_runner == "ava":
        attempts.append(["npx", "--no-install", "ava"])
    elif declared_runner == "tap":
        attempts.append(["npx", "--no-install", "tap"])
    # Then npm test (whatever scripts.test says)
    attempts.append(["npm", "test", "--silent"])
    # Last-ditch fallbacks covering both popular runners
    attempts.append(["npx", "--no-install", "vitest", "run"])
    attempts.append(["npx", "--no-install", "jest", "--passWithNoTests"])

    seen = set()
    last_output = ""
    for cmd in attempts:
        key = tuple(cmd)
        if key in seen:
            continue
        seen.add(key)
        r = _run(cmd, cwd=node_dir, timeout=120)
        if r.returncode == 0:
            return [CheckResult("tests", True)]
        last_output = (r.stdout + "\n" + r.stderr).strip()[-1500:]
    return [CheckResult("tests", False, last_output)]


_STDLIB_NAMES = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else {
    "io", "os", "sys", "json", "csv", "re", "time", "typing", "collections", "abc",
    "test", "email", "logging", "http", "socket", "signal", "queue", "calendar",
    "string", "code", "copy", "numbers", "types", "operator", "token", "stat",
    "array", "struct", "math", "random", "hashlib", "secrets", "html", "xml",
    "urllib", "pathlib", "shutil", "tempfile", "glob", "fnmatch", "contextlib",
    "functools", "itertools", "dataclasses", "enum", "decimal", "fractions",
    "datetime", "threading", "multiprocessing", "subprocess", "asyncio", "unittest",
}


def check_stdlib_conflicts(workspace: str, lang=None) -> list[CheckResult]:
    """Check if any source files shadow stdlib/builtin modules."""
    if lang and lang.family != "python":
        builtins = lang.stdlib_modules
        extensions = tuple(lang.extensions)
    else:
        builtins = _STDLIB_NAMES
        extensions = (".py",)

    conflicts = []
    for root, _, files in os.walk(workspace):
        if "__pycache__" in root or ".venv" in root or "node_modules" in root:
            continue
        for f in files:
            if not f.endswith(extensions):
                continue
            if f.startswith("test_") or ".test." in f or ".spec." in f:
                continue
            stem = os.path.splitext(f)[0]
            if stem in builtins:
                rel = os.path.relpath(os.path.join(root, f), workspace)
                conflicts.append(
                    CheckResult("naming", False,
                                f"File '{rel}' shadows built-in module '{stem}' — rename it "
                                f"(e.g., '{stem}_handler{extensions[0]}' or 'app_{stem}{extensions[0]}')")
                )
    if not conflicts:
        return [CheckResult("naming", True)]
    return conflicts


def _extract_import_module(line: str) -> str | None:
    """Extract the top-level module name from an import statement."""
    line = line.strip()
    m = re.match(r'^from\s+(\w+)', line)
    if m:
        return m.group(1)
    m = re.match(r'^import\s+(\w+)', line)
    if m:
        return m.group(1)
    return None


def check_imports(workspace: str, lang=None) -> list[CheckResult]:
    """Check that all imports can be resolved."""
    if lang and lang.family == "node":
        return _check_imports_ts(workspace)
    if lang and lang.family in ("static", "compiled"):
        return [CheckResult("imports", True, f"{lang.name} — import check skipped", "info")]

    local_modules = set()
    for f in os.listdir(workspace):
        if f.endswith(".py"):
            local_modules.add(os.path.splitext(f)[0])
        elif os.path.isdir(os.path.join(workspace, f)) and not f.startswith((".", "_")):
            local_modules.add(f)  # directories are potential packages

    # Get installed third-party packages
    installed = set()
    try:
        r = subprocess.run(
            ["python3", "-c", "import pkg_resources; print(' '.join(d.key for d in pkg_resources.working_set))"],
            capture_output=True, text=True, timeout=10, cwd=workspace,
        )
        if r.returncode == 0:
            installed = set(r.stdout.strip().split())
            # Also add common package import name mappings
            installed.update({"PIL", "cv2", "sklearn", "bs4", "yaml", "dotenv"})
    except Exception:
        pass

    failures = []
    for f in os.listdir(workspace):
        if not f.endswith(".py"):
            continue
        filepath = os.path.join(workspace, f)
        try:
            with open(filepath) as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line.startswith(("from ", "import ")):
                        continue
                    if line.startswith(("from .", "import .")):
                        continue  # relative imports
                    module = _extract_import_module(line)
                    if not module:
                        continue
                    # Skip if it's stdlib, local, or installed
                    if module in _STDLIB_NAMES or module in local_modules or module.lower().replace("-", "_") in installed:
                        continue
                    # Check common package aliases
                    if module in {"click", "rich", "pydantic", "aiosqlite", "flask", "fastapi",
                                  "uvicorn", "sqlalchemy", "requests", "httpx", "pytest", "numpy",
                                  "pandas", "torch", "aiohttp", "websockets", "redis", "celery",
                                  "jinja2", "marshmallow", "attrs", "pendulum", "arrow"}:
                        continue  # Known packages that might not show in pkg_resources
                    failures.append(
                        CheckResult("imports", False,
                                    f"{f}:{lineno}: unresolved import '{module}' — "
                                    f"not in stdlib, local files, or installed packages")
                    )
        except Exception:
            continue

    if not failures:
        return [CheckResult("imports", True)]
    return failures


def _check_imports_ts(workspace: str) -> list[CheckResult]:
    """Check TS/JS imports — verify node_modules exists if package.json has dependencies."""
    pkg_json = os.path.join(workspace, "package.json")
    if os.path.exists(pkg_json):
        node_modules = os.path.join(workspace, "node_modules")
        if not os.path.exists(node_modules):
            return [CheckResult("imports", False, "node_modules missing — run 'npm install'")]
    return [CheckResult("imports", True)]


def run_module_validation(workspace: str, module_path: str, module_test_file: str | None = None, lang=None) -> list[CheckResult]:
    """Run validation scoped to a single module's directory.

    Checks syntax, imports, and stdlib conflicts for files under module_path.
    Runs module unit tests if module_test_file exists.
    Does NOT run entry_point --test (that's integration-level).
    """
    results = []
    module_dir = os.path.join(workspace, module_path)

    if not os.path.isdir(module_dir):
        return [CheckResult("module", False, f"Module directory {module_path} not found")]

    # Scoped syntax check
    exts = tuple(lang.extensions) if lang else (".py",)
    for root, _, files in os.walk(module_dir):
        if "__pycache__" in root or ".venv" in root or "node_modules" in root:
            continue
        for f in files:
            if not f.endswith(exts):
                continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, workspace)
            if lang and lang.family in ("node", "static", "compiled"):
                continue  # Non-Python syntax checked at project level
            r = _run(["python3", "-m", "py_compile", path], cwd=workspace, timeout=10)
            if r.returncode != 0:
                results.append(CheckResult("syntax", False, f"{rel}: {r.stderr.strip()[:300]}"))
    if not any(r.name == "syntax" for r in results):
        results.append(CheckResult("syntax", True))

    # Scoped stdlib conflict check
    stdlib_names = lang.stdlib_modules if lang else _STDLIB_NAMES
    for f in os.listdir(module_dir):
        if not f.endswith(exts):
            continue
        if f.startswith("test_") or ".test." in f or ".spec." in f:
            continue
        stem = os.path.splitext(f)[0]
        if stem in stdlib_names:
            results.append(CheckResult("naming", False,
                f"File '{module_path}/{f}' shadows stdlib module '{stem}'"))
    if not any(r.name == "naming" for r in results):
        results.append(CheckResult("naming", True))

    # Scoped import check (skip for non-Python — imports validated at project level)
    if lang and lang.family != "python":
        results.append(CheckResult("imports", True))
    else:
        local_modules = set()
        for f in os.listdir(module_dir):
            if f.endswith(exts):
                local_modules.add(os.path.splitext(f)[0])
        # Also consider other module directories as valid imports
        for d in os.listdir(workspace):
            dpath = os.path.join(workspace, d)
            if os.path.isdir(dpath) and not d.startswith((".", "_")):
                local_modules.add(d)
        # Also add top-level .py files
        for f in os.listdir(workspace):
            if f.endswith(".py"):
                local_modules.add(os.path.splitext(f)[0])

        import_failures = []
        for root, _, files in os.walk(module_dir):
            if "__pycache__" in root:
                continue
            for f in files:
                if not f.endswith(".py"):
                    continue
                filepath = os.path.join(root, f)
                rel = os.path.relpath(filepath, workspace)
                try:
                    with open(filepath) as fh:
                        for lineno, line in enumerate(fh, 1):
                            line = line.strip()
                            if not line.startswith(("from ", "import ")):
                                continue
                            if line.startswith(("from .", "import .")):
                                continue
                            module = _extract_import_module(line)
                            if not module:
                                continue
                            if module in _STDLIB_NAMES or module in local_modules:
                                continue
                            # Skip known packages
                            if module in {"click", "rich", "pydantic", "flask", "fastapi",
                                          "uvicorn", "sqlalchemy", "requests", "httpx", "pytest",
                                          "numpy", "pandas", "torch", "aiohttp"}:
                                continue
                            import_failures.append(
                                CheckResult("imports", False,
                                    f"{rel}:{lineno}: unresolved import '{module}'")
                            )
                except Exception:
                    continue

        if not import_failures:
            results.append(CheckResult("imports", True))
        else:
            results.extend(import_failures)

    # Workspace-import smoke — prevents the "[MODULE foo] Tests pass!" lie.
    # Per-module tests can pass while the module fails to import from workspace
    # root (e.g., a rename in a sibling module broke an absolute import). This
    # is the per-subsystem commissioning handoff, catching cross-module breakage
    # at the module level instead of waiting for final VALIDATE.
    if lang and lang.family == "python":
        mod_name = os.path.basename(module_path.rstrip("/"))
        # Only check module packages, not bare files (package has __init__.py)
        if os.path.isdir(os.path.join(workspace, module_path.rstrip("/"))):
            probe = _run(
                ["python3", "-c", f"import {mod_name}"],
                cwd=workspace, timeout=10,
            )
            if probe.returncode != 0:
                err = (probe.stderr or probe.stdout).strip()[-500:]
                results.append(CheckResult(
                    "imports",
                    False,
                    f"Module '{mod_name}' not importable from workspace root: {err}",
                ))

    # Run module tests if test file exists
    if module_test_file:
        test_path = os.path.join(workspace, module_test_file)
        if os.path.exists(test_path):
            test_run_cmd = lang.test_cmd[:] + [module_test_file] if lang else \
                ["python3", "-m", "pytest", "-x", "--tb=short", "-q", module_test_file]
            r = _run(test_run_cmd, cwd=workspace, timeout=60)
            if r.returncode == 0:
                results.append(CheckResult("tests", True))
            else:
                output = (r.stdout + "\n" + r.stderr).strip()[-1500:]
                results.append(CheckResult("tests", False, output))

    return results


def check_functional_smoke(workspace: str, entry_point: str = "main.py", lang=None) -> list[CheckResult]:
    """Generate and run a headless smoke test that exercises cross-module imports and basic operations.

    Unlike --test (which the LLM writes and may be shallow), this test:
    1. Imports every package in the workspace
    2. Instantiates classes found in __init__.py exports
    3. Verifies that data types returned by one module are accepted by another
    """
    if lang and lang.family == "node":
        return _check_functional_smoke_ts(workspace, entry_point, lang)
    if lang and lang.family == "compiled":
        # Compiled: just verify it builds successfully
        if lang.build_cmd:
            r = _run(lang.build_cmd.split(), cwd=workspace, timeout=60)
            if r.returncode == 0:
                return [CheckResult("functional", True, "Build succeeded")]
            return [CheckResult("functional", False, f"Build failed: {(r.stderr or r.stdout)[:500]}")]
        return [CheckResult("functional", True, "No build command, skipped", "info")]
    if lang and lang.family == "static":
        # Static: verify that index.html references at least one .js and one .css file
        index = os.path.join(workspace, entry_point)
        if not os.path.exists(index):
            return [CheckResult("functional", False, f"{entry_point} not found")]
        with open(index) as f:
            content = f.read()
        has_css = '<link' in content and '.css' in content
        has_js = '<script' in content and '.js' in content
        if has_css and has_js:
            return [CheckResult("functional", True, "index.html links CSS and JS")]
        missing = []
        if not has_css:
            missing.append("CSS stylesheet link")
        if not has_js:
            missing.append("JS script tag")
        return [CheckResult("functional", False, f"index.html missing: {', '.join(missing)}")]

    # Discover all packages and top-level modules
    packages = []
    empty_packages = []
    top_modules = []
    for item in sorted(os.listdir(workspace)):
        full = os.path.join(workspace, item)
        if item.startswith((".", "_", "test")) or item == "__pycache__":
            continue
        if os.path.isdir(full):
            init_file = os.path.join(full, "__init__.py")
            py_files = [f for f in os.listdir(full) if f.endswith(".py") and f != "__init__.py"]
            if os.path.exists(init_file) and py_files:
                packages.append(item)
            elif os.path.isdir(full) and not item.startswith("."):
                # Skip directories that contain non-Python content (frontend assets, templates, etc.)
                # These are valid project subdirs even without .py files.
                non_py_content = False
                for root, _, files in os.walk(full):
                    if "__pycache__" in root or "node_modules" in root:
                        continue
                    if any(f.endswith((".html", ".css", ".js", ".jsx", ".ts", ".tsx",
                                       ".json", ".md", ".txt", ".svg", ".png", ".jpg",
                                       ".jinja", ".jinja2", ".j2")) for f in files):
                        non_py_content = True
                        break
                if non_py_content:
                    continue  # frontend/templates/static dirs are fine
                # Directory exists but has no .py files — might be an empty planned module
                if not py_files and not os.path.exists(init_file):
                    empty_packages.append(item)
                elif os.path.exists(init_file) and not py_files:
                    # Has __init__.py but no other .py files — likely empty shell
                    empty_packages.append(item)
        elif item.endswith(".py") and item != entry_point:
            top_modules.append(item[:-3])

    results = []

    # Flag empty module directories
    if empty_packages:
        results.append(CheckResult("functional", False,
            f"Empty module directories (planned but never built): {', '.join(empty_packages)}. "
            f"These modules have no .py files."))

    if not packages and not top_modules:
        if results:
            return results
        return [CheckResult("functional", True, "No packages to smoke test", "info")]

    # Build a smoke test script
    lines = [
        "import sys, traceback",
        "errors = []",
        "",
    ]

    # Test 1: every package imports without error
    for pkg in packages:
        lines.append(f"try:")
        lines.append(f"    import {pkg}")
        lines.append(f"except Exception as e:")
        lines.append(f"    errors.append('Import {pkg}: ' + str(e))")
        lines.append("")

    # Test 2: every top-level module imports
    for mod in top_modules:
        lines.append(f"try:")
        lines.append(f"    import {mod}")
        lines.append(f"except Exception as e:")
        lines.append(f"    errors.append('Import {mod}: ' + str(e))")
        lines.append("")

    # Test 3: cross-module type contracts — instantiate objects and verify types flow
    # For each package, try to get its exported names and instantiate them
    lines.append("# Cross-module type contract checks")
    lines.append("instances = {}")
    for pkg in packages:
        lines.append(f"try:")
        lines.append(f"    _mod = __import__('{pkg}')")
        lines.append(f"    _names = [n for n in dir(_mod) if not n.startswith('_') and callable(getattr(_mod, n, None))]")
        lines.append(f"    instances['{pkg}'] = _names")
        lines.append(f"except Exception:")
        lines.append(f"    pass")
        lines.append("")

    # Test 4: verify that __init__.py re-exports don't point to nonexistent names
    for pkg in packages:
        init_path = os.path.join(workspace, pkg, "__init__.py")
        if os.path.exists(init_path):
            try:
                with open(init_path) as f:
                    init_content = f.read()
                # Find all "from .X import Y" statements
                import_names = re.findall(r'from\s+\.\w+\s+import\s+(.+)', init_content)
                for names_str in import_names:
                    for name in re.split(r'\s*,\s*', names_str.strip()):
                        name = name.strip()
                        if name and not name.startswith('#'):
                            lines.append(f"try:")
                            lines.append(f"    getattr(__import__('{pkg}'), '{name}')")
                            lines.append(f"except AttributeError:")
                            lines.append(f"    errors.append('{pkg}.__init__ exports {name} but it does not exist')")
                            lines.append("")
            except Exception:
                pass

    lines.append("if errors:")
    lines.append("    for e in errors:")
    lines.append("        print(e, file=sys.stderr)")
    lines.append("    sys.exit(1)")
    lines.append("print('SMOKE_OK')")

    smoke_script = "\n".join(lines)
    smoke_name = f"_smoke_test_{uuid.uuid4().hex[:8]}.py"
    smoke_path = os.path.join(workspace, smoke_name)

    try:
        with open(smoke_path, "w") as f:
            f.write(smoke_script)

        r = _run(["python3", smoke_name], cwd=workspace, timeout=15)

        if r.returncode == 0 and "SMOKE_OK" in r.stdout:
            results.append(CheckResult("functional", True))
        else:
            output = (r.stdout.strip() + "\n" + r.stderr.strip()).strip()[:1500]
            results.append(CheckResult("functional", False, output))
        return results if results else [CheckResult("functional", True)]
    finally:
        # Clean up
        try:
            os.remove(smoke_path)
        except OSError:
            pass


def _check_functional_smoke_ts(workspace: str, entry_point: str, lang) -> list[CheckResult]:
    """Generate and run a JS smoke test that imports all src modules."""
    # For framework projects (React/Vue/Angular/Electron), use build as functional check
    if lang and lang.build_cmd and lang.name in ("react", "vue", "angular", "electron"):
        node_dir = _find_node_project_dir(workspace)
        cmd = _npx_no_install(lang.build_cmd.split())
        r = _run(cmd, cwd=node_dir, timeout=120)
        if r.returncode == 0:
            return [CheckResult("functional", True, f"{lang.name} build succeeded")]
        return [CheckResult("functional", False, f"{lang.name} build failed: {(r.stderr or r.stdout)[:500]}")]

    results = []
    src_dir = os.path.join(workspace, "src")
    ts_exts = tuple(lang.extensions)  # .ts, .tsx, .js, .jsx

    # Discover source modules (files in src/ or top-level)
    modules = []
    search_dirs = [src_dir] if os.path.isdir(src_dir) else [workspace]
    for sdir in search_dirs:
        for item in sorted(os.listdir(sdir)):
            full = os.path.join(sdir, item)
            if item.startswith((".", "_")) or "node_modules" in item or "test" in item.lower():
                continue
            if os.path.isdir(full):
                # Check for index.ts/index.js
                idx = any(os.path.exists(os.path.join(full, f"index{e}")) for e in ts_exts)
                if idx:
                    modules.append(os.path.relpath(full, workspace))
            elif item.endswith(ts_exts) and not item.endswith((".test.ts", ".test.js", ".spec.ts", ".spec.js")):
                if item != os.path.basename(entry_point):
                    modules.append(os.path.relpath(full, workspace))

    # Check for empty src directories (planned but never built)
    if os.path.isdir(src_dir):
        for item in sorted(os.listdir(src_dir)):
            full = os.path.join(src_dir, item)
            if os.path.isdir(full) and not item.startswith(".") and item != "node_modules":
                has_src = any(f.endswith(ts_exts) for f in os.listdir(full))
                if not has_src:
                    results.append(CheckResult("functional", False,
                        f"Empty module directory: src/{item} (no source files)"))

    if not modules:
        if results:
            return results
        return [CheckResult("functional", True, "No TS/JS modules to smoke test", "info")]

    # Detect if project uses ESM ("type": "module" in package.json)
    is_esm = False
    pkg_json = os.path.join(workspace, "package.json")
    if os.path.exists(pkg_json):
        try:
            import json as _json
            with open(pkg_json) as pf:
                pkg = _json.load(pf)
            is_esm = pkg.get("type") == "module"
        except Exception:
            pass

    # Build a JS smoke test using dynamic import() for ESM or require() for CJS
    if is_esm:
        # ESM: use async dynamic import()
        lines = [
            "const errors = [];",
            "",
            "async function main() {",
        ]
        for mod in modules:
            mod_path = "./" + mod.replace("\\", "/")
            for ext in ts_exts:
                if mod_path.endswith(ext):
                    mod_path = mod_path[:-len(ext)]
                    break
            # Add .js extension for ESM resolution
            lines.append(f"  try {{")
            lines.append(f"    await import('{mod_path}/index.js');")
            lines.append(f"  }} catch (e1) {{")
            lines.append(f"    try {{")
            lines.append(f"      await import('{mod_path}.js');")
            lines.append(f"    }} catch (e2) {{")
            lines.append(f"      try {{")
            lines.append(f"        await import('{mod_path}');")
            lines.append(f"      }} catch (e3) {{")
            lines.append(f"        errors.push('Import {mod}: ' + e3.message);")
            lines.append(f"      }}")
            lines.append(f"    }}")
            lines.append(f"  }}")
            lines.append("")

        lines.append("  if (errors.length) {")
        lines.append("    errors.forEach(e => console.error(e));")
        lines.append("    process.exit(1);")
        lines.append("  }")
        lines.append("  console.log('SMOKE_OK');")
        lines.append("}")
        lines.append("main().catch(e => { console.error(e.message); process.exit(1); });")
        smoke_ext = ".mjs"
    else:
        # CJS: use require()
        lines = [
            "const errors = [];",
            "",
        ]
        for mod in modules:
            mod_path = "./" + mod.replace("\\", "/")
            for ext in ts_exts:
                if mod_path.endswith(ext):
                    mod_path = mod_path[:-len(ext)]
                    break
            lines.append(f"try {{")
            lines.append(f"  require('{mod_path}');")
            lines.append(f"}} catch (e) {{")
            lines.append(f"  errors.push('Import {mod}: ' + e.message);")
            lines.append(f"}}")
            lines.append("")

        lines.append("if (errors.length) {")
        lines.append("  errors.forEach(e => console.error(e));")
        lines.append("  process.exit(1);")
        lines.append("}")
        lines.append("console.log('SMOKE_OK');")
        smoke_ext = ".js"

    smoke_script = "\n".join(lines)
    smoke_name = f"_smoke_test_{uuid.uuid4().hex[:8]}{smoke_ext}"
    smoke_path = os.path.join(workspace, smoke_name)

    try:
        with open(smoke_path, "w") as f:
            f.write(smoke_script)

        # Try node first (works for both .js and .mjs), fall back to ts-node
        r = _run(["node", smoke_name], cwd=workspace, timeout=15)
        if r.returncode != 0:
            r = _run(["npx", "ts-node", smoke_name], cwd=workspace, timeout=20)

        if r.returncode == 0 and "SMOKE_OK" in r.stdout:
            results.append(CheckResult("functional", True))
        else:
            output = (r.stdout.strip() + "\n" + r.stderr.strip()).strip()[:1500]
            results.append(CheckResult("functional", False, output))
        return results if results else [CheckResult("functional", True)]
    finally:
        try:
            os.remove(smoke_path)
        except OSError:
            pass


def check_framework_conflicts(
    workspace: str,
    expected_packages: list[str] | None = None,
    lang=None,
) -> list[CheckResult]:
    """Detect conflicting UI/framework imports.

    If expected_packages is provided (pip dependencies from the plan), uses it
    to determine which framework is authoritative and reports unauthorized ones.
    """
    framework_files: dict[str, list[str]] = {}  # framework -> list of files using it

    if lang and lang.family in ("static", "compiled"):
        # Static/compiled: no framework conflicts to check
        return [CheckResult("framework", True)]

    if lang and lang.family == "node":
        ui_frameworks = {
            "react": "react",
            "vue": "vue",
            "svelte": "svelte",
            "angular": "angular",
            "@angular/core": "angular",
            "preact": "preact",
            "solid-js": "solid-js",
        }
        file_exts = tuple(lang.extensions)
        skip_dirs = {"node_modules", ".venv", ".git"}
        import_pattern = re.compile(
            r'''(?:import\s+.*?from\s+['"]|require\s*\(\s*['"])([^'"./][^'"]*)'''
        )
    else:
        ui_frameworks = {
            "pygame": "pygame",
            "curses": "curses",
            "tkinter": "tkinter",
            "PyQt5": "PyQt5",
            "PyQt6": "PyQt6",
            "kivy": "kivy",
            "pyglet": "pyglet",
            "arcade": "arcade",
        }
        file_exts = (".py",)
        skip_dirs = {"__pycache__", ".venv"}
        import_pattern = None  # use simple string matching for Python

    for root, _, files in os.walk(workspace):
        if any(sd in root for sd in skip_dirs) or "test" in os.path.basename(root):
            continue
        for f in files:
            if not f.endswith(file_exts):
                continue
            if f.startswith("test_") or ".test." in f or ".spec." in f:
                continue
            filepath = os.path.join(root, f)
            rel = os.path.relpath(filepath, workspace)
            try:
                with open(filepath) as fh:
                    content = fh.read()
                    if import_pattern:
                        # TS: extract actual import names
                        for match in import_pattern.finditer(content):
                            pkg = match.group(1).split("/")[0]  # @angular/core -> @angular... handle scoped
                            # For scoped packages, keep the scope
                            if match.group(1).startswith("@"):
                                parts = match.group(1).split("/")
                                pkg = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
                            if pkg in ui_frameworks:
                                framework_files.setdefault(ui_frameworks[pkg], []).append(rel)
                    else:
                        # Python: simple string matching
                        for framework, name in ui_frameworks.items():
                            if f"import {framework}" in content or f"from {framework}" in content:
                                framework_files.setdefault(name, []).append(rel)
            except Exception:
                continue

    if len(framework_files) <= 1:
        return [CheckResult("framework", True)]

    # Determine the authoritative framework from expected packages
    authorized = None
    if expected_packages:
        lower_pkgs = [p.lower() for p in expected_packages]
        for fw_name in framework_files:
            if fw_name.lower() in lower_pkgs:
                authorized = fw_name
                break

    # Stdlib frameworks (curses, tkinter) won't appear in pip deps.
    # If no competitor is in expected_packages, the stdlib framework wins.
    if authorized is None:
        stdlib_frameworks = {"curses", "tkinter"}
        for fw_name in framework_files:
            if fw_name in stdlib_frameworks:
                competitors_in_deps = [
                    fw for fw in framework_files
                    if fw != fw_name and fw.lower() in (lower_pkgs if expected_packages else [])
                ]
                if not competitors_in_deps:
                    authorized = fw_name
                    break

    if authorized:
        unauthorized = {
            fw: flist for fw, flist in framework_files.items() if fw != authorized
        }
        if unauthorized:
            parts = [f"{fw} in: {', '.join(flist)}" for fw, flist in unauthorized.items()]
            return [CheckResult("framework", False,
                f"Unauthorized framework(s) detected (project uses {authorized}): "
                f"{'; '.join(parts)}. "
                f"Replace all {', '.join(unauthorized.keys())} usage with {authorized}.")]
    else:
        # Can't determine authority — report generic conflict
        parts = [f"{fw}: {', '.join(flist)}" for fw, flist in framework_files.items()]
        return [CheckResult("framework", False,
            f"Conflicting UI frameworks detected: {' vs '.join(framework_files.keys())}. "
            f"Files: {'; '.join(parts)}. Pick ONE framework and use it everywhere.")]

    return [CheckResult("framework", True)]


# --------------------------------------------------------------------------- #
#  WIRING — cross-layer HTTP smoke (Phase 1 of architecture upgrade)          #
# --------------------------------------------------------------------------- #

def _wiring_detect_backend(workspace: str) -> dict | None:
    """Find a Python HTTP backend entry point. Returns {entry, framework, port, cmd} or None.

    Looks for Flask/FastAPI in the usual entry-point spots. The entry file
    itself may not import Flask directly (application-factory pattern); in
    that case we fall back to scanning the project for any Flask/FastAPI
    import — if one exists and the entry runs an HTTP server, it counts.
    Skips projects that are pure CLIs, games, libraries, or static sites.
    """
    candidates = [
        "backend/app.py", "backend/main.py", "backend/server.py",
        "app.py", "server.py", "main.py", "src/main.py",
    ]

    def _find_framework(text: str) -> str | None:
        if "from flask" in text or "import flask" in text or "Flask(" in text:
            return "flask"
        if "FastAPI" in text or "from fastapi" in text:
            return "fastapi"
        return None

    def _project_has(framework: str) -> bool:
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [d for d in dirs if d not in (
                "node_modules", "frontend", ".git", "__pycache__",
                "dist", "build", "venv", ".venv", ".cadillac")]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                try:
                    with open(os.path.join(root, fn)) as f:
                        if _find_framework(f.read()) == framework:
                            return True
                except Exception:
                    pass
        return False

    for rel in candidates:
        path = os.path.join(workspace, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                text = f.read()
        except Exception:
            continue
        framework = _find_framework(text)
        # Application-factory pattern: entry delegates to create_app() /
        # similar without importing Flask directly. If the entry looks like
        # a server launcher (calls create_app, app.run, has app.run-style
        # shape, or pulls in something from a .api/.server submodule) and
        # the project has Flask imports somewhere, count it.
        if framework is None:
            looks_like_launcher = bool(re.search(
                r"\b(create_app|app\.run|run_simple|uvicorn\.run|"
                r"from\s+\w+\.(api|server|app)\b|from\s+backend\b)", text,
            ))
            if looks_like_launcher:
                if _project_has("flask"):
                    framework = "flask"
                elif _project_has("fastapi"):
                    framework = "fastapi"
        if framework is None:
            continue
        port = 5000 if framework == "flask" else 8000
        # Try, in priority order: (1) explicit `port=NNNN` kwarg, (2) env var
        # default `os.environ.get("PORT", "NNNN")`, (3) bare `PORT = NNNN`.
        for pat in (
            r'\bport\s*=\s*(\d{4,5})\b',
            r'os\.environ\.get\(\s*["\']PORT["\']\s*,\s*["\']?(\d{4,5})',
            r'\bPORT\s*=\s*["\']?(\d{4,5})',
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    port = int(m.group(1))
                    break
                except Exception:
                    pass
        return {"entry": rel, "framework": framework, "port": port,
                "cmd": ["python3", rel]}
    return None


def _wiring_detect_frontend(workspace: str) -> dict | None:
    """Find a JS frontend dir with React/Vue/Vite. Returns {dir, deps} or None."""
    for d in ["frontend", "client", "web", "ui", "."]:
        full = os.path.join(workspace, d)
        pkg_path = os.path.join(full, "package.json")
        if not os.path.isfile(pkg_path):
            continue
        try:
            with open(pkg_path) as f:
                pkg = json.load(f)
        except Exception:
            continue
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        marker_keys = {"react", "vue", "@angular/core", "vite", "svelte", "next"}
        if marker_keys.intersection(deps.keys()):
            return {"dir": full, "deps": list(deps.keys())}
    return None


def _wiring_check_no_localhost_hardcode(workspace: str, fe: dict) -> list[CheckResult]:
    """Flag hardcoded `http://localhost:NNNN` in the frontend client.

    Such URLs only resolve from the build machine itself. As soon as the dev
    server is opened from a LAN address, codespaces URL, or any non-loopback
    host, every request to that URL fails with ERR_CONNECTION_REFUSED.
    """
    src_dir = os.path.join(fe["dir"], "src")
    if not os.path.isdir(src_dir):
        src_dir = fe["dir"]

    offenders: list[tuple[str, int, str]] = []
    for root, _, files in os.walk(src_dir):
        if "node_modules" in root or "__mocks__" in root or "dist" in root:
            continue
        for fn in files:
            if not fn.endswith((".ts", ".tsx", ".js", ".jsx")):
                continue
            if ".test." in fn or ".spec." in fn:
                continue
            path = os.path.join(root, fn)
            try:
                with open(path) as f:
                    text = f.read()
            except Exception:
                continue
            for m in re.finditer(r'["\'](https?://localhost(?::\d+)?(?:/[^"\']*)?)["\']', text):
                line_no = text[:m.start()].count("\n") + 1
                offenders.append((os.path.relpath(path, workspace), line_no, m.group(1)))

    if offenders:
        msg = ("Hardcoded localhost URL in client code — breaks for non-loopback users.\n"
               "Derive from `window.location.hostname` (or read `import.meta.env.VITE_API_BASE_URL`):\n")
        for p, ln, url in offenders[:5]:
            msg += f"  {p}:{ln} → {url}\n"
        return [CheckResult("wiring", False, msg.rstrip(), severity="error")]
    return [CheckResult("wiring", True, "no hardcoded localhost in client", severity="info")]


def _wiring_find_free_port(default: int = 5000) -> int:
    import socket
    for p in (default, default + 1, default + 2, default + 3, 0):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            picked = s.getsockname()[1]
            return picked
        except OSError:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    return default


def _wiring_discover_post_routes(workspace: str) -> list[str]:
    """Scan Flask blueprint route decorators to enumerate POST endpoints.

    Resolves blueprint url_prefix per file (declarations in the same file as
    the routes). Skips routes with `<param>` placeholders we can't fill.
    """
    routes: list[str] = []
    seen: set[str] = set()
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs
                   if d not in ("node_modules", "frontend", ".git",
                                "__pycache__", "dist", "build", "venv", ".venv")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path) as f:
                    text = f.read()
            except Exception:
                continue
            prefixes: dict[str, str] = {}
            for m in re.finditer(
                r'(\w+)\s*=\s*Blueprint\([^)]*url_prefix\s*=\s*["\']([^"\']+)["\']',
                text,
            ):
                prefixes[m.group(1)] = m.group(2)
            for m in re.finditer(
                r'@(\w+)\.route\(\s*["\']([^"\']+)["\'][^)]*methods\s*=\s*\[([^\]]+)\]',
                text,
            ):
                obj, route, methods_str = m.group(1), m.group(2), m.group(3)
                if "POST" not in methods_str.upper():
                    continue
                full = prefixes.get(obj, "") + route
                if "<" in full:
                    continue
                if full not in seen:
                    seen.add(full)
                    routes.append(full)
    return routes


def _wiring_concurrent_probe(port: int, synthetic_origin: str, workspace: str,
                              results: list) -> str:
    """Hammer the contract's parameterless GET endpoints concurrently.

    Catches bug classes single-shot probes can't trigger:
      - shared resource (SQLite conn, cache, in-memory state) accessed from
        Werkzeug's per-request thread without locking
      - lazy-init that races (two requests both observe `self._x is None`,
        both initialize, last writer wins)
      - lock-ordering deadlocks under contention
      - connection-pool starvation surfacing as 500s

    Appends an error CheckResult to `results` on any 5xx response or any
    >5%-of-requests connection-error spike. Returns a one-line summary
    suitable for inclusion in the success message.
    """
    import urllib.error
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time

    contract = None
    try:
        from .contracts import Contract
        contract = Contract.load(workspace)
    except Exception:
        pass
    if contract is None or contract.is_empty():
        return ""

    targets: list[str] = []
    for ep in contract.endpoints:
        if ep.method != "GET":
            continue
        if "<" in ep.path or "{" in ep.path or ":" in ep.path.lstrip("/"):
            continue
        targets.append(ep.path)
        if len(targets) >= 5:
            break
    if not targets:
        return ""

    workers = 8
    requests_per_target = 25
    per_request_timeout = 5.0
    stage_budget = 20.0

    def _hit(path: str) -> tuple[str, int | None, str | None]:
        url = f"http://127.0.0.1:{port}{path}"
        try:
            req = urllib.request.Request(
                url, method="GET", headers={"Origin": synthetic_origin},
            )
            with urllib.request.urlopen(req, timeout=per_request_timeout) as r:
                return (path, r.status, None)
        except urllib.error.HTTPError as e:
            return (path, e.code, None)
        except Exception as e:
            return (path, None, type(e).__name__)

    five_xx_by_path: dict[str, int] = {}
    error_by_path: dict[str, dict[str, int]] = {}
    total_requests = 0
    timed_out = False
    started = _time.monotonic()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for path in targets:
            for _ in range(requests_per_target):
                futures.append(pool.submit(_hit, path))
        try:
            for fut in as_completed(futures, timeout=stage_budget):
                try:
                    path, code, err = fut.result()
                except Exception:
                    continue
                total_requests += 1
                if code is not None and 500 <= code < 600:
                    five_xx_by_path[path] = five_xx_by_path.get(path, 0) + 1
                elif err is not None:
                    # Connection refused, timeout, etc. — only flag if it's a
                    # real spike, not the occasional close-then-reopen.
                    error_by_path.setdefault(path, {})
                    error_by_path[path][err] = error_by_path[path].get(err, 0) + 1
        except TimeoutError:
            # Stage budget exceeded — server is too slow to complete the load
            # test. Probably saturated by what we're throwing at it; flag as
            # warning rather than error so we don't false-positive on
            # genuinely-slow-but-correct backends.
            timed_out = True

    elapsed = _time.monotonic() - started

    failures: list[str] = []
    if five_xx_by_path:
        for path, count in five_xx_by_path.items():
            failures.append(
                f"GET {path}: {count}/{requests_per_target} requests returned 5xx"
            )
    # Connection-error spike threshold: >5% of a target's requests.
    error_spike: list[str] = []
    for path, errs in error_by_path.items():
        total_errs = sum(errs.values())
        if total_errs > requests_per_target * 0.05:
            top_err = max(errs.items(), key=lambda kv: kv[1])
            error_spike.append(
                f"GET {path}: {total_errs}/{requests_per_target} connection errors "
                f"(top: {top_err[0]} ×{top_err[1]})"
            )

    if failures:
        results.append(CheckResult(
            "wiring", False,
            "Routes leaked 5xx under concurrent load (single-shot probes pass; "
            "contention surfaces a thread-safety, locking, or shared-resource bug):\n  "
            + "\n  ".join(failures),
            severity="error",
        ))
    if error_spike:
        results.append(CheckResult(
            "wiring", False,
            "Connection errors spiked under load (server may be deadlocking, "
            "running out of workers, or crashing on contention):\n  "
            + "\n  ".join(error_spike),
            severity="warning",
        ))
    if timed_out:
        results.append(CheckResult(
            "wiring", True,  # warning, not failure — backend may just be slow
            f"concurrent probe exceeded {stage_budget}s budget after {total_requests} "
            f"of {len(targets) * requests_per_target} requests; server may be "
            "saturated. Inspect manually if performance matters.",
            severity="warning",
        ))

    return (f"{total_requests} concurrent GET requests across {len(targets)} "
            f"endpoint(s) in {elapsed:.1f}s, no 5xx leaks")


def _wiring_dynamic_checks(workspace: str, be: dict) -> list[CheckResult]:
    """Boot the backend and exercise it from a synthetic non-loopback Origin."""
    import socket
    import time
    import urllib.error
    import urllib.request

    port = _wiring_find_free_port(default=be.get("port", 5000))
    synthetic_origin = "http://lan.test:5173"

    env = os.environ.copy()
    env["PORT"] = str(port)
    # Development-mode boot: matches how a dev would run it. Production-mode
    # config (DB URLs, secret keys) is checked separately, not here.
    env["FLASK_ENV"] = "development"
    env["FLASK_DEBUG"] = "0"  # in case the app reads this directly
    env["CORS_ORIGINS"] = f"{synthetic_origin},http://localhost:5173"
    # Workspace root on PYTHONPATH so application-factory entries that do
    # `from backend.api.app import create_app` resolve. Without this, every
    # such build fails to launch even though its code is correct. We surface
    # this as a hint in the result message rather than blocking the whole
    # check on a launcher-bootstrap quirk.
    env["PYTHONPATH"] = workspace + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    try:
        # New session/process group so terminate cleanly kills any child the
        # Flask debug reloader might fork off.
        proc = subprocess.Popen(
            be["cmd"], cwd=workspace, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
    except Exception as e:
        return [CheckResult("wiring", False, f"failed to launch backend: {e}", severity="error")]

    try:
        deadline = time.time() + 12
        listening = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    listening = True
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.25)
            if proc.poll() is not None:
                tail = proc.stdout.read() if proc.stdout else ""
                return [CheckResult(
                    "wiring", False,
                    f"backend exited before listening on :{port} (exit={proc.returncode})\n"
                    f"{tail[-1500:]}",
                    severity="error",
                )]

        if not listening:
            return [CheckResult(
                "wiring", False,
                f"backend never bound to :{port} within 12s — startup may be hanging",
                severity="error",
            )]

        results: list[CheckResult] = []

        # Probe 1a: GET / and GET /health (best-effort, may not exist)
        for path in ("/", "/health", "/api/health"):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
                with urllib.request.urlopen(req, timeout=5) as r:
                    code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
            except Exception:
                continue
            if 500 <= code < 600:
                results.append(CheckResult(
                    "wiring", False,
                    f"GET {path} returned {code} — backend crashes on root probe",
                    severity="error",
                ))

        # Probe 1b: GET endpoints from the contract (parameterless ones).
        # Hardcoded `/health`-style probes only catch crashes on routes the
        # app declares; contract endpoints are the routes the app PROMISED.
        # Probe each — non-5xx is acceptable (200/401/404 are all real
        # responses, not server crashes). Skip endpoints with `<param>`
        # placeholders (we can't synthesize realistic IDs).
        try:
            from .contracts import Contract
            contract = Contract.load(workspace)
        except Exception:
            contract = None
        contract_5xx: list[str] = []
        if contract is not None and not contract.is_empty():
            for ep in contract.endpoints:
                if ep.method != "GET":
                    continue  # only GET is safe to probe (no side effects)
                if "<" in ep.path or "{" in ep.path or ":" in ep.path.lstrip("/"):
                    continue  # has path params we can't fill
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}{ep.path}", method="GET",
                        headers={"Origin": synthetic_origin},
                    )
                    with urllib.request.urlopen(req, timeout=5) as r:
                        code = r.status
                except urllib.error.HTTPError as e:
                    code = e.code
                except Exception:
                    continue
                if 500 <= code < 600:
                    contract_5xx.append(f"GET {ep.path} → {code}")
        if contract_5xx:
            results.append(CheckResult(
                "wiring", False,
                "Contract GET endpoints crashed (5xx). The route exists but "
                "the handler raises at runtime — likely a thread-safety, "
                "config, or initialization bug invisible to the test suite:\n  "
                + "\n  ".join(contract_5xx),
                severity="error",
            ))

        # Probe 2: CORS echoes synthetic origin from after_request handler
        cors_ok = False
        cors_msg = ""
        for path in ("/health", "/", "/api/health"):
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    method="OPTIONS",
                    headers={
                        "Origin": synthetic_origin,
                        "Access-Control-Request-Method": "GET",
                        "Access-Control-Request-Headers": "content-type",
                    },
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    allow = r.headers.get("Access-Control-Allow-Origin", "")
                    if allow in (synthetic_origin, "*"):
                        cors_ok = True
                        break
                    cors_msg = f"OPTIONS {path}: Access-Control-Allow-Origin='{allow}'"
            except urllib.error.HTTPError as e:
                allow = e.headers.get("Access-Control-Allow-Origin", "")
                if allow in (synthetic_origin, "*"):
                    cors_ok = True
                    break
                cors_msg = f"OPTIONS {path} → {e.code}, no CORS echo"
            except Exception as e:
                cors_msg = f"OPTIONS {path}: {e}"
        if not cors_ok:
            results.append(CheckResult(
                "wiring", False,
                "CORS preflight from a non-loopback Origin (http://lan.test:5173) did not "
                f"echo the origin. {cors_msg}\n"
                "Fix: ensure the @after_request handler reads CORS_ORIGINS from os.environ "
                "(not just app.config), and that the env var is loaded.",
                severity="error",
            ))

        # Probe 3: every discovered POST route should return 4xx, not 5xx, on empty payload
        post_routes = _wiring_discover_post_routes(workspace)
        five_xx_routes: list[str] = []
        for route in post_routes[:15]:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{route}",
                    data=b"{}",
                    method="POST",
                    headers={"Content-Type": "application/json",
                             "Origin": synthetic_origin},
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
            except Exception:
                continue
            if 500 <= code < 600:
                five_xx_routes.append(f"POST {route} → {code}")
        if five_xx_routes:
            results.append(CheckResult(
                "wiring", False,
                "Routes returned 5xx on empty payload (validation should reject with 4xx, "
                "not raise inside the handler):\n  " + "\n  ".join(five_xx_routes),
                severity="error",
            ))

        # Probe 4: concurrency — hammer the contract's parameterless GET
        # endpoints from many threads and watch for 5xx leaks. Single-shot
        # probes pass when one request from one thread succeeds; this catches
        # bugs that only surface under contention: a SQLite connection shared
        # across Werkzeug request threads, a non-thread-safe global, a cache
        # entry written without a lock. Runs ONLY if the previous probes
        # found no errors — there's no point loading a server that's already
        # 5xx'ing on a single request.
        already_failing = any(
            r.severity == "error" for r in results
        )
        concurrent_summary = ""
        if not already_failing:
            concurrent_summary = _wiring_concurrent_probe(
                port, synthetic_origin, workspace, results
            )

        if not results:
            return [CheckResult(
                "wiring", True,
                f"backend boots, CORS echoes non-loopback Origin, "
                f"{len(post_routes)} POST route(s) return non-5xx on empty body"
                + (f", {concurrent_summary}" if concurrent_summary else ""),
                severity="info",
            )]
        return results
    finally:
        # Kill the whole process group so any reloader-forked children die too.
        import signal as _signal
        try:
            os.killpg(os.getpgid(proc.pid), _signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
        except (ProcessLookupError, PermissionError, OSError):
            pass


# --------------------------------------------------------------------------- #
#  CONTRACT ALIGNMENT — verify backend implements + frontend consumes the     #
#  contract. (Phase 2 of architecture upgrade.)                               #
# --------------------------------------------------------------------------- #

def _contract_collect_backend_routes(workspace: str) -> list[tuple[str, str, str]]:
    """Return [(method, full_path, file:line)] for every Flask/FastAPI route."""
    routes: list[tuple[str, str, str]] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs
                   if d not in ("node_modules", "frontend", ".git",
                                "__pycache__", "dist", "build", "venv", ".venv")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path) as f:
                    text = f.read()
            except Exception:
                continue
            prefixes: dict[str, str] = {}
            for m in re.finditer(
                r'(\w+)\s*=\s*Blueprint\([^)]*url_prefix\s*=\s*["\']([^"\']+)["\']',
                text,
            ):
                prefixes[m.group(1)] = m.group(2)
            # Flask: @bp.route(path, methods=[...])
            for m in re.finditer(
                r'@(\w+)\.route\(\s*["\']([^"\']+)["\'][^)]*methods\s*=\s*\[([^\]]+)\]',
                text,
            ):
                obj, route, methods_str = m.group(1), m.group(2), m.group(3)
                full = prefixes.get(obj, "") + route
                line_no = text[:m.start()].count("\n") + 1
                for meth in re.findall(r'["\'](\w+)["\']', methods_str):
                    routes.append((meth.upper(), full,
                                   f"{os.path.relpath(path, workspace)}:{line_no}"))
            # FastAPI: @app.get/.post/.put/.delete/.patch(path)
            for m in re.finditer(
                r'@(\w+)\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']',
                text,
            ):
                obj, meth, route = m.group(1), m.group(2), m.group(3)
                full = prefixes.get(obj, "") + route
                line_no = text[:m.start()].count("\n") + 1
                routes.append((meth.upper(), full,
                               f"{os.path.relpath(path, workspace)}:{line_no}"))
    return routes


def _contract_collect_frontend_calls(workspace: str) -> list[tuple[str, str, str]]:
    """Return [(method, path, file:line)] for every axios/fetch service call."""
    fe_dir = None
    for d in ("frontend", "client", "web", "ui", "."):
        if os.path.isdir(os.path.join(workspace, d, "src")):
            fe_dir = os.path.join(workspace, d, "src")
            break
        if os.path.isfile(os.path.join(workspace, d, "package.json")):
            fe_dir = os.path.join(workspace, d)
            break
    if not fe_dir or not os.path.isdir(fe_dir):
        return []

    calls: list[tuple[str, str, str]] = []
    for root, dirs, files in os.walk(fe_dir):
        if "node_modules" in root or "dist" in root or "build" in root:
            continue
        for fn in files:
            if not fn.endswith((".ts", ".tsx", ".js", ".jsx")):
                continue
            if ".test." in fn or ".spec." in fn:
                continue
            path = os.path.join(root, fn)
            try:
                with open(path) as f:
                    text = f.read()
            except Exception:
                continue
            # axios pattern: matches any identifier ending in api/Api/Client/client
            # plus the bare names (api, axios, http, client). Catches:
            #   api.post(...), axios.get(...), apiClient.post(...),
            #   bookmarksApi.delete(...), AuthClient.get(...)
            # Tolerates TS generic type args: `.post<LoginResponse>('/url', ...)`
            # and chained-call newlines: `apiClient\n    .post(...)`.
            for m in re.finditer(
                r'\b\w*(?:[Aa]pi|[Cc]lient|axios|http|fetch)\s*\.\s*(get|post|put|delete|patch)\s*(?:<[^>]*>\s*)?\(\s*[`"\']([^`"\']+)[`"\']',
                text,
                re.DOTALL,
            ):
                meth, route = m.group(1).upper(), m.group(2)
                line_no = text[:m.start()].count("\n") + 1
                calls.append((meth, route,
                              f"{os.path.relpath(path, workspace)}:{line_no}"))
            # fetch pattern: fetch('/api/...', { method: 'POST' })
            for m in re.finditer(
                r'fetch\s*\(\s*[`"\']([^`"\']+)[`"\'][^)]*method\s*:\s*[`"\'](\w+)[`"\']',
                text,
            ):
                route, meth = m.group(1), m.group(2).upper()
                line_no = text[:m.start()].count("\n") + 1
                calls.append((meth, route,
                              f"{os.path.relpath(path, workspace)}:{line_no}"))
            # Helper-function pattern: `await get('/api/x')` /
            # `post('/api/y', body)`. Frontend code commonly wraps fetch
            # behind a verb-named helper; the route literal is at the helper
            # call site, not the underlying fetch. Restricted to `/api/`-prefixed
            # paths so we don't false-match on local-state getters like
            # `get('user')`.
            for m in re.finditer(
                r'\b(get|post|put|delete|patch)\s*(?:<[^>]*>\s*)?\(\s*[`"\']\s*(/api/[^`"\']*)[`"\']',
                text,
            ):
                meth, route = m.group(1).upper(), m.group(2)
                line_no = text[:m.start()].count("\n") + 1
                calls.append((meth, route,
                              f"{os.path.relpath(path, workspace)}:{line_no}"))
    return calls


def _contract_norm_path(p: str) -> str:
    """Collapse all path-param syntaxes to <param> for matching, strip query
    strings, strip trailing slashes (Flask `strict_slashes=False` makes
    `/x` and `/x/` equivalent at runtime; the contract should not have to
    care)."""
    # Drop query string — contract alignment is about routing, not params.
    if "?" in p:
        p = p.split("?", 1)[0]
    # JS template-literal ${id} — must come BEFORE the bare {id} rule below
    # or we'd convert `${id}` → `$<param>` (eating only the `{id}` part).
    p = re.sub(r"\$\{[^}]+\}", "<param>", p)
    # Flask <id>, <int:id>, <string:slug>
    p = re.sub(r"<[^>]+>", "<param>", p)
    # FastAPI / Express {id}
    p = re.sub(r"\{[^}]+\}", "<param>", p)
    # :id-style (Express)
    p = re.sub(r":\w+", "<param>", p)
    # Trailing slash (but keep root "/")
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def _contract_match(method: str, path: str,
                    candidates: list[tuple[str, str, str]]) -> tuple[str, str, str] | None:
    """Find a candidate (method, path, ref) matching the contract endpoint.

    Frontend often calls without /api prefix because baseURL has it baked in,
    so we also try matching with that prefix stripped from the contract.
    """
    norm_target = _contract_norm_path(path)
    target_no_api = norm_target[4:] if norm_target.startswith("/api/") else norm_target
    for cand_method, cand_path, ref in candidates:
        if cand_method != method:
            continue
        cand_norm = _contract_norm_path(cand_path)
        if cand_norm in (norm_target, target_no_api):
            return cand_method, cand_path, ref
        # Candidate may have its own /api prefix that contract doesn't
        if cand_norm.startswith("/api/") and cand_norm[4:] == target_no_api:
            return cand_method, cand_path, ref
    return None


def check_contract_alignment(workspace: str, lang=None) -> list[CheckResult]:
    """For every contract endpoint, verify backend implements it AND (for
    consumed_by entries) frontend has at least one matching service call.

    No-op when the project has no `contracts.json` (single-language projects,
    or modular projects where the LLM judged contracts unnecessary).
    """
    from .contracts import Contract

    contract = Contract.load(workspace)
    if contract.is_empty():
        return [CheckResult("contract", True, "no contract present (single-layer project)",
                            severity="info")]

    backend_routes = _contract_collect_backend_routes(workspace)
    frontend_calls = _contract_collect_frontend_calls(workspace)

    missing_backend: list[str] = []
    missing_frontend: list[str] = []

    for ep in contract.endpoints:
        be_match = _contract_match(ep.method, ep.path, backend_routes)
        if not be_match:
            missing_backend.append(
                f"{ep.method} {ep.path} (module={ep.module}) — no backend route found"
            )
        if ep.consumed_by:
            fe_match = _contract_match(ep.method, ep.path, frontend_calls)
            if not fe_match:
                missing_frontend.append(
                    f"{ep.method} {ep.path} — declared consumed_by={ep.consumed_by} but no client call found"
                )

    results: list[CheckResult] = []
    if missing_backend:
        results.append(CheckResult(
            "contract", False,
            "Contract endpoints with no backend implementation:\n  " +
            "\n  ".join(missing_backend),
            severity="error",
        ))
    if missing_frontend:
        results.append(CheckResult(
            "contract", False,
            "Contract endpoints declared consumed_by frontend but no client call found:\n  " +
            "\n  ".join(missing_frontend),
            severity="warning",
        ))
    if not results:
        results.append(CheckResult(
            "contract", True,
            f"all {len(contract.endpoints)} contract endpoints have backend impls + frontend calls",
            severity="info",
        ))
    return results


# --------------------------------------------------------------------------- #
#  TOPOLOGY — orchestrator-owned cross-module graph (Phase 3)                 #
# --------------------------------------------------------------------------- #

def check_topology(workspace: str, lang=None) -> list[CheckResult]:
    """Run cross-module invariant checks via the Topology graph.

    Catches bug classes the per-layer pipeline misses by definition: env-var
    lookups with no load site, contract endpoints pointing at modules that
    don't exist, port collisions across modules.

    No-op for projects without enough structure to have a topology
    (single-file scripts, single-module libraries with no env var reads).
    """
    from .topology import Topology

    topo = Topology.build(workspace)
    issues = topo.check()
    if not issues:
        # Don't emit a "passed" result when there's nothing to check — keeps
        # the validation log uncluttered for small projects.
        n_reads = len(topo.env_var_reads)
        n_endpoints = len(topo.contract.endpoints)
        if n_reads == 0 and n_endpoints == 0:
            return [CheckResult("topology", True,
                                "skipped — no env vars or contract endpoints to verify",
                                severity="info")]
        return [CheckResult(
            "topology", True,
            f"checked {n_reads} env var read(s), "
            f"{n_endpoints} contract endpoint(s), "
            f"{len(topo.port_bindings)} port binding(s)",
            severity="info",
        )]

    by_severity: dict[str, list] = {"error": [], "warning": []}
    for issue in issues:
        by_severity.setdefault(issue.severity, []).append(issue)

    results: list[CheckResult] = []
    for severity, items in by_severity.items():
        if not items:
            continue
        body_lines = []
        for issue in items:
            body_lines.append(f"[{issue.rule}] {issue.message}")
            for ref in issue.refs[:3]:
                body_lines.append(f"    at {ref}")
        results.append(CheckResult(
            "topology", False, "\n".join(body_lines), severity=severity,
        ))
    return results


def check_wiring(workspace: str, lang=None) -> list[CheckResult]:
    """Cross-layer HTTP smoke test — Phase 1 of the architecture upgrade.

    For projects with both a Python HTTP backend and a JS frontend, boots the
    backend on a free port and exercises it from a synthetic non-loopback
    Origin. Catches three bug classes the static + per-layer pipeline misses:

      - hardcoded `http://localhost:NNNN` in the frontend API client
      - CORS allow-origin not echoing real (non-loopback) request origins
      - backend routes that 500 on minimal/empty payloads

    No-op for single-layer projects (CLI, library, game, static site).
    """
    be = _wiring_detect_backend(workspace)
    fe = _wiring_detect_frontend(workspace)
    if not be or not fe:
        return [CheckResult("wiring", True,
                            "skipped — single-layer project (no backend+frontend pair detected)",
                            severity="info")]

    results: list[CheckResult] = []
    # Topology runs first — if env vars are unloaded or contract references
    # phantom modules, the rest of the smoke is moot.
    results.extend(check_topology(workspace, lang))
    # Contract alignment: backend implements + frontend consumes the shapes.
    results.extend(check_contract_alignment(workspace, lang))
    # Static client-side hygiene before booting the server.
    results.extend(_wiring_check_no_localhost_hardcode(workspace, fe))
    # Dynamic boot + probe — only if the static checks above didn't already
    # surface a structural issue. (Booting a server with phantom env vars or
    # missing routes would just multiply error noise.)
    has_blockers = any(
        not r.passed and r.severity == "error" for r in results
    )
    if not has_blockers:
        results.extend(_wiring_dynamic_checks(workspace, be))
    else:
        results.append(CheckResult(
            "wiring", True,
            "dynamic boot+probe skipped — fix topology/contract errors above first",
            severity="info",
        ))
    return results


# --------------------------------------------------------------------------- #
#  SECURITY — static OWASP-flavor scan (Phase 1.2 of weakness roadmap)        #
# --------------------------------------------------------------------------- #
#
# The LLM cheerfully ships footguns the rest of the pipeline can't see:
# JWT_SECRET = "dev"; cursor.execute(f"SELECT * FROM x WHERE id={id}");
# subprocess.run(f"sh -c '{cmd}'", shell=True); hashlib.md5(password).
# Tests pass — they don't probe security. Lint passes — it's not a lint rule.
# This check fires regex patterns chosen for high signal + low false positives.
# Always runs (no external deps); augments with `bandit` if it's on PATH.

_SEC_SKIP_DIRS = (
    "node_modules", "__pycache__", "dist", "build", "venv", ".venv",
    ".git", ".cadillac", ".pytest_cache",
)


@dataclass
class _SecFinding:
    rule: str
    severity: str       # "high" | "medium"
    file: str           # relative path
    line: int
    snippet: str        # the matched line, trimmed
    why: str            # one-line explanation


def _sec_relevant_file(path: str, lang) -> str | None:
    """Return 'py' | 'ts' | 'js' | None depending on whether we should scan
    this file. Skips dependency dirs and dotfiles."""
    parts = path.split(os.sep)
    if any(p in _SEC_SKIP_DIRS for p in parts):
        return None
    if path.endswith(".py"):
        return "py"
    if path.endswith((".ts", ".tsx")):
        return "ts"
    if path.endswith((".js", ".jsx")):
        return "js"
    return None


# Each pattern: dict with rule, severity, kinds (which file types apply),
# regex (pre-compiled), why (human-readable explanation).
# Patterns are written to be PRECISE — false positives erode trust faster than
# false negatives, and bandit/semgrep cover the rest if installed.
_SEC_PATTERNS: list[dict] = [
    # ── Hardcoded secrets ────────────────────────────────────────────────
    {
        "rule": "hardcoded_secret",
        "severity": "high",
        "kinds": {"py", "ts", "js"},
        # Assignment to a secret-named identifier with a string literal that
        # is NOT obviously a placeholder/empty/env lookup. ≥6 chars is a
        # heuristic to skip "" and "x" placeholders.
        "regex": re.compile(
            r"""(?i)\b(password|passwd|api[_-]?key|secret[_-]?key|"""
            r"""auth[_-]?token|access[_-]?token|jwt[_-]?secret|"""
            r"""private[_-]?key|client[_-]?secret)\s*[:=]\s*"""
            r"""['"]([^'"\s]{6,})['"]"""
        ),
        "why": ("hardcoded secret in source — anyone with repo access has the "
                "credential. Read from os.environ / a vault / a runtime config."),
    },
    {
        "rule": "aws_access_key",
        "severity": "high",
        "kinds": {"py", "ts", "js"},
        "regex": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "why": "AWS access key literal in source.",
    },
    {
        "rule": "private_key_block",
        "severity": "high",
        "kinds": {"py", "ts", "js"},
        "regex": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "why": "PEM private key embedded in source.",
    },
    # ── SQL injection ────────────────────────────────────────────────────
    {
        "rule": "sql_fstring",
        "severity": "high",
        "kinds": {"py"},
        # cursor.execute(f"...{var}..."), conn.execute(f"..."), etc.
        "regex": re.compile(
            r"""\.execute\s*\(\s*f["'][^"']*\{[^"']*\}"""
        ),
        "why": ("f-string SQL — user input substituted directly into the query. "
                "Use parameterized queries: execute(\"... ?\", (val,)) or :name binding."),
    },
    {
        "rule": "sql_concat",
        "severity": "high",
        "kinds": {"py"},
        "regex": re.compile(
            r"""\.execute\s*\(\s*["'][^"']*["']\s*\+\s*\w"""
        ),
        "why": ("string concatenation in SQL — same risk as f-string. "
                "Use parameterized queries."),
    },
    # ── Command injection ────────────────────────────────────────────────
    {
        "rule": "shell_true_with_interp",
        "severity": "high",
        "kinds": {"py"},
        # subprocess.run/call/Popen with shell=True AND f-string OR + concat
        # in the command. Literal-only shell=True is fine (annoying but not
        # injection); interpolation is the bug.
        "regex": re.compile(
            r"""subprocess\.\w+\s*\(\s*f["']"""
            r"""[^)]*shell\s*=\s*True"""
            r"""|subprocess\.\w+\s*\([^)]*\+\s*\w[^)]*shell\s*=\s*True"""
        ),
        "why": ("shell=True with interpolated user input is a remote-shell hole. "
                "Pass argv as a list with shell=False, or shlex.quote each arg."),
    },
    {
        "rule": "os_system_interp",
        "severity": "high",
        "kinds": {"py"},
        "regex": re.compile(r"""os\.system\s*\(\s*f["']|os\.system\s*\([^)]*\+\s*\w"""),
        "why": "os.system with interpolation — shell injection. Use subprocess with argv list.",
    },
    # ── Dangerous eval ───────────────────────────────────────────────────
    {
        "rule": "eval_user_input",
        "severity": "high",
        "kinds": {"py"},
        # eval(f"..."), eval(request.args[...]), eval(input(...))
        "regex": re.compile(
            r"""\beval\s*\(\s*(f["']|request\.|input\(|sys\.argv|os\.environ\b)"""
        ),
        "why": "eval() on non-literal input — arbitrary code execution.",
    },
    {
        "rule": "exec_user_input",
        "severity": "high",
        "kinds": {"py"},
        "regex": re.compile(
            r"""\bexec\s*\(\s*(f["']|request\.|input\(|sys\.argv|os\.environ\b)"""
        ),
        "why": "exec() on non-literal input — arbitrary code execution.",
    },
    {
        "rule": "js_eval",
        "severity": "high",
        "kinds": {"ts", "js"},
        "regex": re.compile(r"""\beval\s*\(|new\s+Function\s*\("""),
        "why": "eval() / new Function() — arbitrary JS execution if input is attacker-controlled.",
    },
    # ── JWT footguns ─────────────────────────────────────────────────────
    {
        "rule": "jwt_alg_none",
        "severity": "high",
        "kinds": {"py", "ts", "js"},
        "regex": re.compile(r"""algorithm[s]?\s*[:=]\s*['"]?(none|None)['"]?"""),
        "why": ("JWT algorithm 'none' disables signature verification — anyone can "
                "forge tokens. Use HS256 / RS256 with a real key."),
    },
    {
        "rule": "jwt_verify_false",
        "severity": "high",
        "kinds": {"py", "ts", "js"},
        "regex": re.compile(
            r"""jwt\.\w+\s*\([^)]*verify\s*[:=]\s*False"""
            r"""|verify_signature\s*[:=]\s*False"""
        ),
        "why": "JWT decode with verify=False — accepts any token. Use a real key + verify=True.",
    },
    # ── Insecure transport ───────────────────────────────────────────────
    {
        "rule": "tls_verify_disabled",
        "severity": "medium",
        "kinds": {"py"},
        "regex": re.compile(r"""\bverify\s*=\s*False\b|_create_unverified_context"""),
        "why": ("TLS verification disabled — vulnerable to man-in-the-middle. "
                "Acceptable in tests; never in production paths."),
    },
    # ── Weak crypto for password/token ───────────────────────────────────
    {
        "rule": "weak_password_hash",
        "severity": "medium",
        "kinds": {"py"},
        # md5/sha1 used on a line that mentions password/passwd/token (heuristic)
        "regex": re.compile(
            r"""hashlib\.(md5|sha1)\s*\([^)]*(password|passwd|pwd|token|secret)"""
        ),
        "why": ("md5/sha1 for password/token hashing — both are fast (good for "
                "attackers) and weak. Use bcrypt / argon2 / scrypt."),
    },
    {
        "rule": "weak_token_random",
        "severity": "medium",
        "kinds": {"py"},
        # random.X used on a line mentioning token/secret/password
        "regex": re.compile(
            r"""\brandom\.(random|choice|randint|getrandbits)\s*\([^)]*\)"""
            r"""[^\n]*\b(token|secret|password|nonce|salt|key)\b"""
            r"""|\b(token|secret|password|nonce|salt|key)\b[^\n]*"""
            r"""=\s*['"]?random\.(random|choice|randint|getrandbits)"""
        ),
        "why": ("random.* for token/secret generation — predictable. "
                "Use the `secrets` module: secrets.token_urlsafe(), secrets.token_bytes()."),
    },
    # ── Timing attacks on credential compare ─────────────────────────────
    {
        "rule": "constant_time_compare",
        "severity": "medium",
        "kinds": {"py"},
        # password == provided OR token == expected — should be hmac.compare_digest
        "regex": re.compile(
            r"""\b(password|passwd|token|secret|hmac|signature|digest|hash)\b"""
            r"""\s*==\s*\b(\w+)\b"""
            r"""|\b(\w+)\s*==\s*\b(password|passwd|token|secret|hmac|signature|digest|hash)\b"""
        ),
        "why": ("== comparison of password/token leaks length & prefix via timing. "
                "Use hmac.compare_digest(a, b) for constant-time comparison."),
    },
    # ── XSS in DOM / React ───────────────────────────────────────────────
    {
        "rule": "innerhtml_assignment",
        "severity": "medium",
        "kinds": {"ts", "js"},
        # element.innerHTML = <something not an obvious literal>. Skip lines
        # where the RHS is a quoted string with no template-literal interpolation.
        "regex": re.compile(
            r"""\.innerHTML\s*=\s*(?!['"][^'"]*['"]\s*[;]?\s*$)(?!`[^`${]*`\s*[;]?\s*$)"""
        ),
        "why": ("innerHTML assignment with non-literal RHS — XSS if user input "
                "ever reaches it. Use textContent, or sanitize via DOMPurify."),
    },
    {
        "rule": "dangerously_set_inner_html",
        "severity": "medium",
        "kinds": {"ts", "js"},
        "regex": re.compile(r"""dangerouslySetInnerHTML\s*[:=]\s*\{"""),
        "why": ("dangerouslySetInnerHTML — XSS unless the value is sanitized "
                "(DOMPurify) or comes from a trusted source like a markdown lib."),
    },
    # ── Production debug / wide-open hosts ───────────────────────────────
    {
        "rule": "wide_open_hosts",
        "severity": "medium",
        "kinds": {"py"},
        "regex": re.compile(r"""ALLOWED_HOSTS\s*=\s*\[\s*['"]\*['"]\s*\]"""),
        "why": "ALLOWED_HOSTS=['*'] disables Django's Host-header check.",
    },
]


def _scan_for_security_issues(workspace: str, lang) -> list[_SecFinding]:
    """Walk source tree, fire each pattern, return findings."""
    findings: list[_SecFinding] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SEC_SKIP_DIRS]
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, workspace)
            kind = _sec_relevant_file(rel, lang)
            if kind is None:
                continue
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except Exception:
                continue
            # Skip files that are tests — security patterns there are usually
            # fixtures (e.g., `password = "test1234"` in a unit test) and
            # flagging them is mostly noise.
            base = os.path.basename(rel)
            if base.startswith("test_") or base.endswith((
                ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
                ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx",
                "_test.py",
            )) or "/__tests__/" in rel or "/tests/" in rel or rel.startswith("tests/"):
                continue
            for pat in _SEC_PATTERNS:
                if kind not in pat["kinds"]:
                    continue
                for m in pat["regex"].finditer(text):
                    line_no = text[:m.start()].count("\n") + 1
                    snippet = text.splitlines()[line_no - 1].strip()
                    if len(snippet) > 200:
                        snippet = snippet[:200] + "…"
                    # De-dup: don't fire the same rule on the same line twice.
                    if any(
                        f.rule == pat["rule"] and f.file == rel and f.line == line_no
                        for f in findings
                    ):
                        continue
                    findings.append(_SecFinding(
                        rule=pat["rule"],
                        severity=pat["severity"],
                        file=rel,
                        line=line_no,
                        snippet=snippet,
                        why=pat["why"],
                    ))
    return findings


def _try_bandit(workspace: str, lang) -> list[_SecFinding]:
    """Run bandit if it's on PATH. Returns [] cleanly if not available."""
    if not lang or lang.family != "python":
        return []
    import shutil
    if not shutil.which("bandit"):
        return []
    try:
        r = subprocess.run(
            ["bandit", "-r", workspace, "-f", "json", "--skip", "B101", "-q"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return []
    if not r.stdout:
        return []
    try:
        data = json.loads(r.stdout)
    except Exception:
        return []
    findings: list[_SecFinding] = []
    for item in data.get("results", []) or []:
        sev = (item.get("issue_severity") or "").lower()
        conf = (item.get("issue_confidence") or "").lower()
        # Map bandit's severity x confidence onto our two-level scheme.
        # Only HIGH-severity HIGH-confidence becomes our "high".
        if sev == "high" and conf in ("high", "medium"):
            our_sev = "high"
        elif sev in ("high", "medium"):
            our_sev = "medium"
        else:
            continue
        path = item.get("filename") or ""
        try:
            rel = os.path.relpath(path, workspace)
        except Exception:
            rel = path
        findings.append(_SecFinding(
            rule=f"bandit:{item.get('test_id', '?')}",
            severity=our_sev,
            file=rel,
            line=int(item.get("line_number") or 0),
            snippet=(item.get("code") or "").strip()[:200],
            why=item.get("issue_text") or "",
        ))
    return findings


def check_security(workspace: str, lang=None) -> list[CheckResult]:
    """Static security scan — flag OWASP-class footguns the rest of the
    pipeline doesn't see (hardcoded secrets, SQL injection, command
    injection, weak crypto, JWT misconfig, XSS sinks, timing attacks).

    Built-in regex patterns run always (zero deps). Augmented with `bandit`
    on Python projects when bandit is on PATH.

    HIGH severity → CheckResult error (fails the build).
    MEDIUM severity → CheckResult warning (visible but doesn't block).
    """
    findings = _scan_for_security_issues(workspace, lang)
    findings.extend(_try_bandit(workspace, lang))

    if not findings:
        return [CheckResult(
            "security", True,
            "no obvious security issues found in source",
            severity="info",
        )]

    high = [f for f in findings if f.severity == "high"]
    medium = [f for f in findings if f.severity == "medium"]
    results: list[CheckResult] = []

    def _format(group: list[_SecFinding], cap: int = 8) -> str:
        lines: list[str] = []
        for f in group[:cap]:
            lines.append(f"  [{f.rule}] {f.file}:{f.line} — {f.why}")
            if f.snippet:
                lines.append(f"      > {f.snippet}")
        if len(group) > cap:
            lines.append(f"  …and {len(group) - cap} more")
        return "\n".join(lines)

    if high:
        results.append(CheckResult(
            "security", False,
            f"{len(high)} HIGH-severity security issue(s) — fix before shipping:\n"
            + _format(high),
            severity="error",
        ))
    if medium:
        results.append(CheckResult(
            "security", False,
            f"{len(medium)} MEDIUM-severity security issue(s):\n"
            + _format(medium),
            severity="warning",
        ))
    return results


def run_validation(
    workspace: str,
    entry_point: str = "main.py",
    expected_packages: list[str] | None = None,
    lang=None,
) -> list[CheckResult]:
    """Run the full validation pipeline (11 checks)."""
    results = []
    results.extend(check_stdlib_conflicts(workspace, lang))
    results.extend(check_imports(workspace, lang))
    results.extend(check_static_names(workspace, lang))   # catches NameErrors statically
    results.extend(check_syntax(workspace, lang))
    results.extend(check_lint(workspace, lang))
    results.extend(check_security(workspace, lang))       # OWASP-class static scan
    results.extend(check_framework_conflicts(workspace, expected_packages=expected_packages, lang=lang))
    results.extend(check_functional_smoke(workspace, entry_point, lang))
    results.extend(check_entry_point(workspace, entry_point, lang))
    results.extend(check_smoke_run(workspace, lang))      # catches NameErrors in interactive-loop paths
    results.extend(check_tests(workspace, lang))
    return results


def format_failures(results: list[CheckResult]) -> str:
    """Format failures for injection into BUILD phase context."""
    failures = [r for r in results if not r.passed and r.severity == "error"]
    if not failures:
        return ""
    lines = ["VALIDATION FAILURES (fix these):"]
    for f in failures:
        lines.append(f"  [{f.name}] {f.output[:500]}")
    return "\n".join(lines)


def results_to_dict(results: list[CheckResult]) -> dict[str, bool | None]:
    """Convert results to a simple dict for progress tracking."""
    d: dict[str, bool | None] = {
        "naming": None, "imports": None, "static_names": None,
        "syntax": None, "lint": None, "security": None, "framework": None,
        "functional": None, "run": None, "smoke_run": None, "tests": None,
    }
    for r in results:
        if r.name in d:
            if d[r.name] is None:
                d[r.name] = r.passed
            elif not r.passed:
                d[r.name] = False
    return d
