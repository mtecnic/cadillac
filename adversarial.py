"""Adversarial test generation — Phase 1.3 of the weakness roadmap.

Standard pattern: LLM writes the implementation AND the tests. Result: the
LLM tests its own assumptions. Boundary conditions, error paths, edge cases
get glossed over because the LLM is biased toward proving its own work
correct.

Adversarial pass: a SECOND LLM turn whose only job is to write tests
*designed to break* the first LLM's implementation. Different mental model,
different test coverage. Catches the bug class that ships when one author
both writes the code and the tests for it.

For v1 this is **advisory** — findings surface in the build log but don't
block the pipeline. False positives (LLM tests something the impl doesn't
promise, hallucinates an API, writes a syntactically broken test file)
would block builds for the wrong reason. Once we have telemetry showing
the false-positive rate is low, we can promote to blocking with a
retry-limited fix loop.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field


_ADVERSARIAL_DIR = ".cadillac/adversarial"  # within the workspace
_MAX_FILES_TO_SHOW = 4
_MAX_BYTES_PER_FILE = 6000
_MAX_TESTS_PER_PASS = 8


@dataclass
class AdversarialResult:
    """Result of one adversarial pass.

    Three states distinguished:
      - passed=True, crashed=False, skipped_reason="" — all tests ran and passed
      - passed=False, crashed=False — tests ran, some failed (real findings)
      - passed=True, crashed=True — adversarial pipeline itself broke (LLM
        output was bad Python, test file failed to load, runner timed out).
        Was previously lumped with skipped_reason and treated as advisory
        info; now surfaced explicitly so the human sees "adversarial broke,
        review the generated file" instead of "adversarial passed silently".
    """
    passed: bool                              # True if no test failures (or no test ran)
    n_tests_run: int = 0
    n_failed: int = 0
    failures: list[str] = field(default_factory=list)  # one-liner per failure
    output: str = ""                          # tail of test runner output
    skipped_reason: str = ""                  # set when nothing was probed
    test_file_path: str = ""                  # for human inspection later
    crashed: bool = False                     # adversarial pipeline itself broke


@dataclass
class ProjectObjectives:
    """What this program is supposed to DO — not just what its code looks like.

    The adversarial pass writes tests against THIS, not against the abstract
    notion of edge cases. A test like "empty input returns empty list" is
    only meaningful if returning an empty list on empty input is a real
    user-visible promise — otherwise it's noise.
    """
    task_text: str = ""                       # the human's original task
    entry_point: str = ""
    architecture_excerpt: str = ""            # relevant slice of architecture.md
    endpoints: list[dict] = field(default_factory=list)  # from contracts.json
    constraints: list[str] = field(default_factory=list)  # explicit musts/must-nots

    def objectives_summary(self) -> list[str]:
        """One-line objective per logical capability the program claims."""
        out: list[str] = []
        for ep in self.endpoints:
            method = ep.get("method", "")
            path = ep.get("path", "")
            name = ep.get("name", "")
            consumer = ", ".join(ep.get("consumed_by", []) or []) or "?"
            out.append(f"{method} {path}  ({name}, used by: {consumer})")
        for c in self.constraints:
            out.append(f"CONSTRAINT: {c}")
        if not out and self.task_text:
            # Best-effort: split task text into sentences as objectives
            sentences = re.split(r"(?<=[.!?])\s+", self.task_text.strip())
            out.extend(s.strip() for s in sentences[:6] if s.strip())
        return out


def _extract_objectives(workspace: str) -> ProjectObjectives:
    """Read plan.json + contracts.json + architecture.md to figure out what
    the program is supposed to DO. The adversarial prompt builds on top of
    this — without it, we'd just be writing generic edge-case noise."""
    o = ProjectObjectives()

    plan_path = os.path.join(workspace, "plan.json")
    if os.path.isfile(plan_path):
        try:
            with open(plan_path) as f:
                plan = json.load(f)
            o.task_text = (plan.get("task") or plan.get("task_text") or "").strip()
            o.entry_point = plan.get("entry_point", "") or ""
            o.constraints = list(plan.get("constraints") or [])
        except Exception:
            pass

    contracts_path = os.path.join(workspace, "contracts.json")
    if os.path.isfile(contracts_path):
        try:
            with open(contracts_path) as f:
                contracts = json.load(f)
            o.endpoints = list(contracts.get("endpoints") or [])
        except Exception:
            pass

    arch_path = os.path.join(workspace, "architecture.md")
    if os.path.isfile(arch_path):
        try:
            with open(arch_path) as f:
                text = f.read()
            # Keep only the "## Requirements" / "## Goals" / "## Capabilities"
            # sections — those describe behavior. Skip implementation details.
            wanted: list[str] = []
            current_keep = False
            keep_headers = re.compile(
                r"^#{1,3}\s+(requirements|goals|capabilities|features|"
                r"user stor|use cases|public api|behavior|invariants)",
                re.IGNORECASE,
            )
            for line in text.splitlines():
                if line.startswith("#"):
                    current_keep = bool(keep_headers.match(line))
                if current_keep:
                    wanted.append(line)
            o.architecture_excerpt = "\n".join(wanted)[:3000]
        except Exception:
            pass

    # Salvage task_text from progress.md if plan.json didn't have it
    if not o.task_text:
        prog_path = os.path.join(workspace, "progress.md")
        if os.path.isfile(prog_path):
            try:
                with open(prog_path) as f:
                    text = f.read(2000)
                m = re.search(r"^#\s+(.+?)$|^Task:\s*(.+?)$", text, re.MULTILINE)
                if m:
                    o.task_text = (m.group(1) or m.group(2) or "").strip()
            except Exception:
                pass

    return o


def run_adversarial_tests(workspace: str, lang, cfg, emit) -> AdversarialResult:
    """Generate adversarial tests via LLM, run them, report findings.

    Returns AdversarialResult. Never raises — but a crash inside the
    pipeline now sets crashed=True so the engine can log it visibly
    rather than passing silently as "advisory info". (Audit M6.)
    """
    try:
        return _run_adversarial_tests_inner(workspace, lang, cfg, emit)
    except Exception as e:
        return AdversarialResult(
            passed=True,
            crashed=True,
            skipped_reason=f"adversarial pass crashed: {type(e).__name__}: {e}",
        )


def _run_adversarial_tests_inner(workspace: str, lang, cfg, emit) -> AdversarialResult:
    if lang is None:
        return AdversarialResult(passed=True, skipped_reason="no language detected")

    # We support python (pytest) and node (vitest) for v1.
    if lang.family not in ("python", "node"):
        return AdversarialResult(
            passed=True,
            skipped_reason=f"adversarial unsupported for family={lang.family}",
        )

    objectives = _extract_objectives(workspace)
    if not objectives.task_text and not objectives.endpoints:
        return AdversarialResult(
            passed=True,
            skipped_reason="no plan.json / contracts.json — can't establish "
                           "what this program is for, refusing to write blind "
                           "edge-case tests",
        )

    targets = _pick_targets(workspace, lang)
    if not targets:
        return AdversarialResult(
            passed=True,
            skipped_reason="no testable source files found",
        )

    impl_text = _format_targets_for_prompt(workspace, targets)
    existing_tests_text = _format_existing_tests_for_prompt(workspace, lang)

    emit("log", msg=(
        f"[ADVERSARIAL] Targeting {len(objectives.objectives_summary())} "
        f"declared objective(s) across {len(targets)} source file(s) "
        f"via second LLM pass..."
    ))

    # Late import to avoid a circular dep — engine.py imports adversarial.
    from .engine import chat

    prompt = _build_prompt(impl_text, existing_tests_text, objectives, lang)
    try:
        msg = chat(cfg, [{"role": "user", "content": prompt}], tools=[], emit=emit)
    except Exception as e:
        return AdversarialResult(
            passed=True,
            skipped_reason=f"LLM call failed: {type(e).__name__}: {e}",
        )

    raw = (msg.get("content") or "").strip()
    test_code = _extract_code_block(raw, lang)
    if not test_code or len(test_code) < 50:
        return AdversarialResult(
            passed=True,
            skipped_reason="LLM did not produce a usable test file",
        )

    test_path = _write_test_file(workspace, lang, test_code)
    emit("log", msg=f"[ADVERSARIAL] Wrote {os.path.relpath(test_path, workspace)}")

    n_run, n_fail, failures, tail = _run_test_file(workspace, lang, test_path)

    if n_run == 0:
        # Test file didn't even import / run. The LLM wrote bad Python
        # (or vitest/pytest crashed). Mark crashed=True so the engine
        # logs this visibly — passing silently as "skipped advisory" was
        # how a broken adversarial pass could ship without anyone
        # noticing. (Audit M6.)
        return AdversarialResult(
            passed=True,
            crashed=True,
            n_tests_run=0,
            skipped_reason="adversarial test file failed to load (LLM "
                           "produced unrunnable test code)",
            output=tail,
            test_file_path=test_path,
        )

    return AdversarialResult(
        passed=(n_fail == 0),
        n_tests_run=n_run,
        n_failed=n_fail,
        failures=failures,
        output=tail,
        test_file_path=test_path,
    )


# ── Target selection ────────────────────────────────────────────────────────

def _pick_targets(workspace: str, lang) -> list[str]:
    """Return up to _MAX_FILES_TO_SHOW relative paths of source files worth
    probing. Heuristics: largest non-test source files in the canonical src
    dirs, with at least one function/class definition (so there's something
    to attack)."""
    candidates: list[tuple[int, str]] = []  # (size, relpath)
    skip_dirs = {
        "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
        ".git", ".cadillac", ".pytest_cache", "tests", "__tests__",
    }
    if lang.family == "python":
        exts = (".py",)
        signal = re.compile(r"^\s*(def|class)\s+\w", re.MULTILINE)
    else:  # node
        exts = (".ts", ".tsx", ".js", ".jsx")
        signal = re.compile(
            r"^\s*(export\s+)?(function|class|const\s+\w+\s*=\s*(async\s+)?\(?\w)",
            re.MULTILINE,
        )

    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if not fn.endswith(exts):
                continue
            base = fn
            if (base.startswith("test_") or base.endswith(("_test.py",
                ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
                ".spec.ts", ".spec.tsx", ".spec.js"))):
                continue
            if base in ("__init__.py", "main.py", "index.ts", "main.ts"):
                # Entry/init files are usually wiring, not logic worth attacking.
                continue
            full = os.path.join(root, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size < 200 or size > 50_000:
                continue
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read(_MAX_BYTES_PER_FILE)
            except Exception:
                continue
            if not signal.search(text):
                continue
            rel = os.path.relpath(full, workspace)
            candidates.append((size, rel))

    # Pick the largest few — biggest surface = most testable behavior
    candidates.sort(key=lambda x: -x[0])
    return [p for _, p in candidates[:_MAX_FILES_TO_SHOW]]


def _format_targets_for_prompt(workspace: str, targets: list[str]) -> str:
    parts: list[str] = []
    for rel in targets:
        full = os.path.join(workspace, rel)
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                content = f.read(_MAX_BYTES_PER_FILE)
        except Exception:
            continue
        parts.append(f"### {rel}\n```\n{content}\n```")
    return "\n\n".join(parts)


def _format_existing_tests_for_prompt(workspace: str, lang) -> str:
    """Show existing test files so the LLM doesn't duplicate. Capped."""
    test_files: list[str] = []
    skip_dirs = {"node_modules", "__pycache__", ".venv", "venv", ".git", ".cadillac"}
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if (fn.startswith("test_") and fn.endswith(".py")) or fn.endswith((
                "_test.py", ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
                ".spec.ts", ".spec.tsx", ".spec.js",
            )):
                test_files.append(os.path.relpath(os.path.join(root, fn), workspace))

    if not test_files:
        return "(no existing tests)"

    # Show 2 representative test files at most, trimmed.
    parts: list[str] = []
    for rel in test_files[:2]:
        try:
            with open(os.path.join(workspace, rel), encoding="utf-8",
                      errors="replace") as f:
                content = f.read(2000)
        except Exception:
            continue
        parts.append(f"### {rel}\n```\n{content}\n```")
    if len(test_files) > 2:
        parts.append(f"...and {len(test_files) - 2} more test file(s)")
    return "\n\n".join(parts)


# ── Prompt ──────────────────────────────────────────────────────────────────

_PROMPT_PYTHON = """\
You are an ADVERSARIAL TESTER. Below you have:

  1. WHAT THE PROGRAM IS FOR — the human's actual task and its declared
     objectives (endpoints, contracts, constraints from the build plan)
  2. THE CODE — the implementation another engineer just wrote
  3. EXISTING TESTS — happy-path tests the implementer wrote for themselves

Your job: **write tests that verify the program ACTUALLY DELIVERS its
declared objectives under hostile real-world conditions**. Not unit-level
noise. Not "empty list returns empty list". Real probes against real promises.

THE OBJECTIVES BELOW ARE THE GROUND TRUTH. If the code doesn't have a function
or endpoint that fulfills an objective, that's a bug worth catching. If the
code has functions that do things OUTSIDE the objectives, ignore them — they're
incidental, not promises.

For each objective, ask:
  - Can a real user hit this and get a correct result?
  - What happens when the input is at the edge (max size, min size, boundary)?
  - What happens when the input is malformed or hostile?
  - What happens when the same operation runs twice (idempotency, duplicates)?
  - What happens under concurrent or interleaved use (if applicable)?
  - Is the data persisted/preserved across the operations the user expects?

TESTS THAT ARE NOISE (don't write these):
  - "Empty input returns empty list" unless emptiness is a documented behavior
  - Testing private/internal helpers the user can't reach
  - Testing the type checker (e.g., calling with wrong type to check TypeError)
  - Trivial getter-returns-what-was-set unless persistence is a real promise
  - Tests that probe APIs the impl doesn't expose

TESTS THAT ARE REAL PROBES (write these instead):
  - "Register two users with the same email — second one rejects with 409"
  - "Place a bid below the current high bid — rejected; high bid unchanged"
  - "Save a bookmark, restart the app simulation, list bookmarks — saved one is there"
  - "Call the rate-limited endpoint 100 times in 10s — at least N are throttled"
  - "Send a 5MB JSON body to a route documented as accepting <1MB — 413, not 500"
  - "Concurrent counter increments from 10 threads — final value matches expected"

CONSTRAINTS:
1. Use **pytest**.
2. Tests must map to ONE OR MORE declared objectives. If you can't trace a test
   to an objective, don't write it.
3. Each test must be self-contained.
4. Cap at {max_tests} tests. Pick the highest-value probes — better 4 tests that
   probe real behavior than 8 that probe nothing.
5. Use correct imports from the structure visible in the code.
6. No live external services. If an objective requires e.g. real OAuth, skip it.

Output ONLY the test file content as Python source. No fences, no commentary.

═══════════════════════════════════════════════════════════════════════════
WHAT THE PROGRAM IS FOR
═══════════════════════════════════════════════════════════════════════════

Task: {task_text}

Declared objectives:
{objectives}

{architecture_block}

═══════════════════════════════════════════════════════════════════════════
CODE
═══════════════════════════════════════════════════════════════════════════

{impl_text}

═══════════════════════════════════════════════════════════════════════════
EXISTING TESTS (don't duplicate these — your job is the GAPS)
═══════════════════════════════════════════════════════════════════════════

{existing_tests}
"""

_PROMPT_NODE = """\
You are an ADVERSARIAL TESTER. Below you have:

  1. WHAT THE PROGRAM IS FOR — declared task and objectives
  2. THE CODE — what another engineer just wrote
  3. EXISTING TESTS — the implementer's happy-path tests

Your job: **write tests that verify the program ACTUALLY DELIVERS its declared
objectives under hostile conditions**. Not unit-level noise. Real probes
against real promises.

Tests must trace to a specific objective. If you can't say "this test verifies
objective X under condition Y", don't write it.

REAL PROBES (write these):
  - End-to-end user flows that exercise a contract endpoint with realistic data
  - Persistence: do values survive the operations the user expects?
  - Concurrency: does N operations interleaved produce a sane final state?
  - Boundary inputs at the documented limits, not arbitrary `""` / `null`
  - Malformed inputs that a hostile user might send
  - Idempotency: same call twice should produce expected behavior

NOISE (don't write):
  - Trivial setter-getter tests
  - Tests of private helpers the user can't reach
  - Tests that probe APIs the code doesn't actually expose

CONSTRAINTS:
1. Use **vitest** (`import {{ describe, it, expect }} from 'vitest'`).
2. Map each test to a declared objective.
3. Self-contained tests, correct imports, no live services.
4. Cap at {max_tests} tests. Quality over quantity.

Output ONLY the test file content as TypeScript. No fences, no commentary.

═══════════════════════════════════════════════════════════════════════════
WHAT THE PROGRAM IS FOR
═══════════════════════════════════════════════════════════════════════════

Task: {task_text}

Declared objectives:
{objectives}

{architecture_block}

═══════════════════════════════════════════════════════════════════════════
CODE
═══════════════════════════════════════════════════════════════════════════

{impl_text}

═══════════════════════════════════════════════════════════════════════════
EXISTING TESTS (don't duplicate)
═══════════════════════════════════════════════════════════════════════════

{existing_tests}
"""


def _build_prompt(impl_text: str, existing_tests_text: str,
                  objectives: ProjectObjectives, lang) -> str:
    template = _PROMPT_PYTHON if lang.family == "python" else _PROMPT_NODE
    obj_lines = objectives.objectives_summary()
    obj_block = "\n".join(f"  - {o}" for o in obj_lines) if obj_lines else "  (none declared)"
    arch_block = ""
    if objectives.architecture_excerpt:
        arch_block = (
            "Architecture excerpt (requirements/goals only):\n"
            + objectives.architecture_excerpt
        )
    return template.format(
        max_tests=_MAX_TESTS_PER_PASS,
        impl_text=impl_text,
        existing_tests=existing_tests_text,
        task_text=objectives.task_text or "(not declared)",
        objectives=obj_block,
        architecture_block=arch_block,
    )


# ── Output handling ─────────────────────────────────────────────────────────

def _extract_code_block(raw: str, lang) -> str:
    """LLMs sometimes wrap output in markdown fences despite being told not to.
    Strip the fence if present, otherwise return raw. Returns the test code."""
    raw = raw.strip()
    # ```python\n...\n``` or ```ts\n...\n``` or ```\n...\n```
    fence_re = re.compile(
        r"^```(?:python|py|typescript|ts|tsx|javascript|js)?\s*\n(.*?)\n```\s*$",
        re.DOTALL,
    )
    m = fence_re.match(raw)
    if m:
        return m.group(1).strip()
    return raw


def _write_test_file(workspace: str, lang, test_code: str) -> str:
    """Write the LLM's test code under .cadillac/adversarial/, return path."""
    out_dir = os.path.join(workspace, _ADVERSARIAL_DIR)
    os.makedirs(out_dir, exist_ok=True)
    if lang.family == "python":
        name = "test_adversarial.py"
    else:
        name = "adversarial.test.ts"
    path = os.path.join(out_dir, name)
    with open(path, "w") as f:
        f.write(test_code)
    return path


# ── Test execution + parsing ────────────────────────────────────────────────

def _run_test_file(workspace: str, lang, test_path: str) -> tuple[int, int, list[str], str]:
    """Run the LLM-written tests, return (n_run, n_failed, failure_summaries, tail)."""
    if lang.family == "python":
        return _run_pytest(workspace, test_path)
    else:
        return _run_vitest(workspace, test_path)


def _run_pytest(workspace: str, test_path: str) -> tuple[int, int, list[str], str]:
    """Run adversarial pytest tests via validate.py's _run() so we get
    history-informed adaptive timeouts. Hardcoded 60s previously meant
    larger projects' adversarial tests timed out repeatedly without
    ever scaling up. (Audit M7.)"""
    from .validate import _run
    rel = os.path.relpath(test_path, workspace)
    r = _run(
        ["python3", "-m", "pytest", rel, "-x", "--tb=short", "-q", "--no-header"],
        cwd=workspace, timeout=60,
    )
    output = (r.stdout or "") + (r.stderr or "")
    if "Timed out" in output and not r.stdout:
        return (0, 0, [], output[-2000:])
    n_passed, n_failed = _parse_pytest_summary(output)
    failures = _extract_pytest_failures(output)
    return (n_passed + n_failed, n_failed, failures, output[-2000:])


def _parse_pytest_summary(output: str) -> tuple[int, int]:
    """Parse pytest's summary line. Examples:
        '====== 5 passed in 0.04s ======'
        '====== 2 failed, 3 passed in 0.04s ======'
    """
    n_passed, n_failed = 0, 0
    m = re.search(r"(\d+)\s+passed", output)
    if m:
        n_passed = int(m.group(1))
    m = re.search(r"(\d+)\s+failed", output)
    if m:
        n_failed = int(m.group(1))
    return n_passed, n_failed


def _extract_pytest_failures(output: str) -> list[str]:
    """Pull one-line summaries of each failing test. Pytest's short tb format
    has 'FAILED path::test_name - ExcType: message' lines."""
    failures: list[str] = []
    for m in re.finditer(r"^FAILED\s+(\S+)(?:\s+-\s+(.+))?$", output, re.MULTILINE):
        path_and_test = m.group(1)
        msg = m.group(2) or ""
        failures.append(f"{path_and_test}  ({msg.strip()[:120]})" if msg
                        else path_and_test)
    return failures


def _run_vitest(workspace: str, test_path: str) -> tuple[int, int, list[str], str]:
    """Run adversarial vitest via validate._run() for adaptive timeouts. (M7)"""
    from .validate import _run
    rel = os.path.relpath(test_path, workspace)
    r = _run(
        ["npx", "vitest", "run", "--reporter=verbose", rel],
        cwd=workspace, timeout=120,
    )
    output = (r.stdout or "") + (r.stderr or "")
    if "Timed out" in output and not r.stdout:
        return (0, 0, [], output[-2000:])
    n_passed, n_failed = _parse_vitest_summary(output)
    failures = _extract_vitest_failures(output)
    return (n_passed + n_failed, n_failed, failures, output[-2000:])


def _parse_vitest_summary(output: str) -> tuple[int, int]:
    """Vitest summary line: 'Tests  3 passed (3)' or 'Tests  2 failed | 3 passed'.

    Anchored to the literal `Tests` label so we don't false-hit the earlier
    `Test Files  1 passed` line which counts files, not tests.
    """
    n_passed, n_failed = 0, 0
    # Find the "Tests" summary line specifically
    m = re.search(r"^\s*Tests\s+(.+)$", output, re.MULTILINE)
    if m:
        line = m.group(1)
        mp = re.search(r"(\d+)\s+passed", line)
        if mp:
            n_passed = int(mp.group(1))
        mf = re.search(r"(\d+)\s+failed", line)
        if mf:
            n_failed = int(mf.group(1))
    return n_passed, n_failed


def _extract_vitest_failures(output: str) -> list[str]:
    """Vitest verbose format prints 'FAIL  path > test name' or '✗ test name'."""
    failures: list[str] = []
    for m in re.finditer(r"^\s*[×✗xFAIL]+\s+(.+?)$", output, re.MULTILINE):
        line = m.group(1).strip()
        if line and len(line) < 200:
            failures.append(line)
    # De-dup while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for f in failures:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq[:8]
