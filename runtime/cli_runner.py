"""CLI runtime verification — drive scripted runs against a built CLI binary.

For Python CLIs, Rust/Go binaries, and anything that takes argv + stdin and
returns an exit code + stdout. Each `ScriptedRun` is one process invocation
with optional setup steps that run sequentially before it (for stateful
CLIs that need init/add/configure before the assertion call).

Catches:
  - CLI exits 0 with wrong output (the test happened to assert on internals)
  - --help works but real subcommands crash
  - flag combinations the test suite never tried
  - stateful CLIs that lose their state between invocations
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field

from .types import Probe, ProbeFailure, VerificationResult


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScriptedRun(Probe):
    """One CLI invocation with assertions.

    `setup` runs first as a tuple of ScriptedRun probes (used for stateful
    CLIs: `init`, then `add user`, then the actual assertion run). Setup
    failures abort the parent run with a clear `detail`.
    """
    argv: tuple[str, ...] = ()
    stdin: str = ""
    expect_exit_code: int = 0
    expect_stdout_contains: tuple[str, ...] = ()
    expect_stdout_not_contains: tuple[str, ...] = ()
    expect_stderr_contains: tuple[str, ...] = ()
    timeout_s: int = 10
    setup: tuple = ()  # tuple of dicts (rendered as ScriptedRun at run-time)


# ── LLM-driven generation ────────────────────────────────────────────────────


_GENERATE_SYSTEM_PROMPT = """You are a senior QA engineer writing scripted \
CLI runs for an autobuilder. Given a spec of user stories and the CLI entry \
point, output runs that prove each story actually works.

Output JSON only — no prose, no fences. Schema:

{
  "runs": [
    {
      "story_id": "S03",
      "title": "user counts bytes in a file",
      "priority": "must",
      "setup": [
        {"argv": ["bash", "-c", "echo hello > /tmp/sample.txt"], "expect_exit_code": 0}
      ],
      "argv": ["python3", "main.py", "count", "/tmp/sample.txt"],
      "stdin": "",
      "expect_exit_code": 0,
      "expect_stdout_contains": ["6"],
      "expect_stdout_not_contains": ["error"],
      "expect_stderr_contains": [],
      "timeout_s": 10
    }
  ]
}

Rules:
- One run per must- and should-priority story. Skip "could" stories.
- argv is the FULL command — start with `python3 main.py ...`, `./bytes ...`, \
or whatever the entry point is. Use absolute paths only when the test needs \
predictable filesystem state (`/tmp/...`).
- `setup` is for state-mutating preparation (creating files, initializing \
config). It runs IN ORDER before the parent run. Setup runs are themselves \
ScriptedRuns with their own assertions.
- `expect_stdout_contains` is a list of substrings ALL of which must appear. \
Use observable outputs, not internals.
- `expect_stdout_not_contains` catches the silent-failure case: command \
exits 0 but the output contains "error", "traceback", "not found", etc.
- Use `timeout_s` larger than 10 only for genuinely slow operations.
- 5-12 runs for typical CLIs. Don't pad.

Output ONLY the JSON object."""


def _build_generate_prompt(spec, workspace: str, lang) -> str:
    entry = getattr(lang, "entry_point", "main.py") if lang else "main.py"
    name = getattr(lang, "name", "") if lang else ""
    family = getattr(lang, "family", "") if lang else ""

    # Sniff how the CLI is invoked
    if family == "python":
        invocation_hint = f"Invoke via `python3 {entry}` from the workspace root."
    elif name == "rust":
        # Read Cargo.toml for the bin name
        bin_name = _rust_bin_name(workspace) or "app"
        invocation_hint = (
            f"This is a Rust CLI. Build with `cargo build --release` first, "
            f"then invoke via `./target/release/{bin_name}`. The runner will "
            f"pre-build the binary; in argv use `./target/release/{bin_name}` "
            f"as the executable path."
        )
    elif name == "go":
        invocation_hint = "This is a Go CLI. Invoke via `go run main.go ...` from the workspace root."
    else:
        invocation_hint = f"Entry point: {entry}"

    spec_block = spec.to_prompt_block(max_chars=4000) if spec else ""
    return f"{invocation_hint}\n\n{spec_block}\n\nGenerate runs."


def _rust_bin_name(workspace: str) -> str | None:
    """Extract the `name = ` from Cargo.toml [package] section."""
    cargo = os.path.join(workspace, "Cargo.toml")
    if not os.path.isfile(cargo):
        return None
    try:
        with open(cargo) as f:
            in_package = False
            for line in f:
                stripped = line.strip()
                if stripped.startswith("[package]"):
                    in_package = True
                    continue
                if stripped.startswith("[") and stripped != "[package]":
                    in_package = False
                if in_package and stripped.startswith("name"):
                    import re
                    m = re.search(r'name\s*=\s*"([^"]+)"', stripped)
                    if m:
                        return m.group(1)
    except OSError:
        return None
    return None


def generate_runs(spec, workspace: str, lang, cfg, emit) -> list[ScriptedRun]:
    """Call the LLM to produce scripted runs. Returns a list (may be empty)."""
    from cadillac.engine import chat, extract_json

    if spec is None or spec.is_empty():
        return []
    user_msg = _build_generate_prompt(spec, workspace, lang)
    messages = [
        {"role": "system", "content": _GENERATE_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    emit("log", msg="[RUNTIME/cli] generating scripted runs from spec...")
    try:
        msg = chat(cfg, messages, tools=[], emit=emit)
    except Exception as e:
        emit("log", msg=f"[RUNTIME/cli] LLM error: {e}")
        return []
    raw = (msg.get("content") or "").strip()
    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        return []
    raw_runs = parsed.get("runs") or []
    if not isinstance(raw_runs, list):
        return []
    runs: list[ScriptedRun] = []
    for entry in raw_runs:
        if not isinstance(entry, dict):
            continue
        r = _run_from_dict(entry)
        if r is not None:
            runs.append(r)

    # Persist for inspection / iterate reuse.
    try:
        cad_dir = os.path.join(workspace, ".cadillac")
        os.makedirs(cad_dir, exist_ok=True)
        with open(os.path.join(cad_dir, "cli_runs.json"), "w") as f:
            json.dump([_run_to_dict(r) for r in runs], f, indent=2)
    except OSError:
        pass

    emit("log", msg=f"[RUNTIME/cli] {len(runs)} run(s) generated")
    return runs


def _run_from_dict(d: dict) -> ScriptedRun | None:
    try:
        story_id = str(d.get("story_id", "")).strip()
        title = str(d.get("title", "")).strip()
        priority = str(d.get("priority", "must")).strip()
        if priority not in ("must", "should", "could"):
            priority = "must"
        argv_raw = d.get("argv") or []
        if not isinstance(argv_raw, list) or not argv_raw:
            return None
        argv = tuple(str(a) for a in argv_raw)
        setup_raw = d.get("setup") or []
        setup: list[ScriptedRun] = []
        for s in setup_raw if isinstance(setup_raw, list) else []:
            if isinstance(s, dict):
                child = _run_from_dict({
                    "story_id": story_id + ".setup",
                    "title": "setup",
                    "priority": "must",
                    **s,
                })
                if child is not None:
                    setup.append(child)
        return ScriptedRun(
            story_id=story_id, title=title, priority=priority,
            argv=argv, stdin=str(d.get("stdin", "")),
            expect_exit_code=int(d.get("expect_exit_code", 0) or 0),
            expect_stdout_contains=tuple(
                str(s) for s in (d.get("expect_stdout_contains") or [])
            ),
            expect_stdout_not_contains=tuple(
                str(s) for s in (d.get("expect_stdout_not_contains") or [])
            ),
            expect_stderr_contains=tuple(
                str(s) for s in (d.get("expect_stderr_contains") or [])
            ),
            timeout_s=int(d.get("timeout_s", 10) or 10),
            setup=tuple(setup),
        )
    except (TypeError, ValueError):
        return None


def _run_to_dict(r: ScriptedRun) -> dict:
    return {
        "story_id": r.story_id, "title": r.title, "priority": r.priority,
        "argv": list(r.argv), "stdin": r.stdin,
        "expect_exit_code": r.expect_exit_code,
        "expect_stdout_contains": list(r.expect_stdout_contains),
        "expect_stdout_not_contains": list(r.expect_stdout_not_contains),
        "expect_stderr_contains": list(r.expect_stderr_contains),
        "timeout_s": r.timeout_s,
        "setup": [_run_to_dict(s) for s in r.setup],
    }


# ── Pre-build hook for compiled CLIs ─────────────────────────────────────────


def _prebuild(workspace: str, lang, emit) -> str:
    """For compiled languages, build the binary before any run. Returns '' on
    success or an error string suitable for a `boot_failure`-style probe."""
    name = getattr(lang, "name", "") if lang else ""
    if name == "rust":
        emit("log", msg="[RUNTIME/cli] cargo build --release...")
        try:
            r = subprocess.run(
                ["cargo", "build", "--release"], cwd=workspace,
                capture_output=True, text=True, timeout=300,
            )
        except FileNotFoundError:
            return "cargo not on PATH; cannot run rust CLI"
        except subprocess.TimeoutExpired:
            return "cargo build timed out after 5 minutes"
        if r.returncode != 0:
            return f"cargo build failed (rc={r.returncode}):\n{(r.stderr or r.stdout)[-1500:]}"
    return ""


# ── Run execution ────────────────────────────────────────────────────────────


def _exec_one(run_obj: ScriptedRun, workspace: str) -> tuple[int, str, str, str]:
    """Execute one ScriptedRun. Returns (exit_code, stdout, stderr, error).
    `error` is non-empty when the process itself crashed (FileNotFoundError,
    timeout, etc.) — distinct from a clean exit with a wrong code."""
    try:
        r = subprocess.run(
            list(run_obj.argv), cwd=workspace,
            input=run_obj.stdin or None,
            capture_output=True, text=True,
            timeout=run_obj.timeout_s,
        )
        return (r.returncode, r.stdout, r.stderr, "")
    except FileNotFoundError as e:
        return (0, "", "", f"executable not found: {e}")
    except subprocess.TimeoutExpired:
        return (0, "", "", f"timed out after {run_obj.timeout_s}s")
    except Exception as e:
        return (0, "", "", f"crashed: {e}")


def run_scripted(run_obj: ScriptedRun, workspace: str) -> ProbeFailure | None:
    """Execute a ScriptedRun + its setup. Returns first failure or None."""
    # Setup phase: all setup steps must succeed
    for idx, s in enumerate(run_obj.setup):
        code, out, err, exec_err = _exec_one(s, workspace)
        if exec_err:
            return ProbeFailure(
                probe=run_obj,
                failure_kind="exit_code_mismatch",
                detail=f"setup step {idx + 1} ({' '.join(s.argv)}) failed: {exec_err}",
                actual=exec_err,
            )
        if code != s.expect_exit_code:
            return ProbeFailure(
                probe=run_obj,
                failure_kind="exit_code_mismatch",
                detail=f"setup step {idx + 1} ({' '.join(s.argv)}): "
                       f"expected exit {s.expect_exit_code}, got {code}",
                actual=(out + err)[-400:],
            )

    # Main run
    code, out, err, exec_err = _exec_one(run_obj, workspace)
    cmd_summary = " ".join(run_obj.argv)
    if exec_err:
        return ProbeFailure(
            probe=run_obj,
            failure_kind="timeout" if "timed out" in exec_err else "exit_code_mismatch",
            detail=f"({cmd_summary}): {exec_err}",
            actual=exec_err,
        )
    if code != run_obj.expect_exit_code:
        return ProbeFailure(
            probe=run_obj,
            failure_kind="exit_code_mismatch",
            detail=f"({cmd_summary}): expected exit {run_obj.expect_exit_code}, got {code}",
            actual=(err or out)[-400:],
        )
    for needle in run_obj.expect_stdout_contains:
        if needle not in out:
            return ProbeFailure(
                probe=run_obj,
                failure_kind="stdout_mismatch",
                detail=f"({cmd_summary}): stdout missing required substring {needle!r}",
                actual=out[-400:],
            )
    for needle in run_obj.expect_stdout_not_contains:
        if needle in out:
            return ProbeFailure(
                probe=run_obj,
                failure_kind="stdout_mismatch",
                detail=f"({cmd_summary}): stdout contains forbidden substring {needle!r}",
                actual=out[-400:],
            )
    for needle in run_obj.expect_stderr_contains:
        if needle not in err:
            return ProbeFailure(
                probe=run_obj,
                failure_kind="stderr_mismatch",
                detail=f"({cmd_summary}): stderr missing required substring {needle!r}",
                actual=err[-400:],
            )
    return None


# ── Strategy entry point ─────────────────────────────────────────────────────


def run(spec, workspace: str, lang, cfg, contract, emit) -> VerificationResult:
    """CLI strategy entry point."""
    build_err = _prebuild(workspace, lang, emit)
    if build_err:
        return VerificationResult(
            strategy="cli", probes_run=0,
            failures=(ProbeFailure(
                probe=Probe(story_id="prebuild", title="binary build",
                             priority="must"),
                failure_kind="boot_failure",
                detail="pre-build failed before any run could execute",
                actual=build_err,
            ),),
        )

    runs = generate_runs(spec, workspace, lang, cfg, emit)
    if not runs:
        return VerificationResult(strategy="cli", probes_run=0,
                                   skipped_reason="no runs generated")

    failures: list[ProbeFailure] = []
    for run_obj in runs:
        emit("log", msg=f"[RUNTIME/cli] {run_obj.story_id}: {run_obj.title}")
        fail = run_scripted(run_obj, workspace)
        if fail is not None:
            emit("log", msg=f"  [fail] {fail.failure_kind} — {fail.detail}")
            failures.append(fail)

    return VerificationResult(
        strategy="cli", probes_run=len(runs), failures=tuple(failures),
    )
