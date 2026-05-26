"""Library runtime verification — drive usage snippets against a built library.

For projects that expose a public API but don't run as a server or CLI:
PyTorch model packages, Python utility libs, Node packages, Rust crates.
Each `UsageExample` is a short snippet that imports from the library, runs
it, and asserts on the result. Snippet exits 0 = pass; non-zero or
ImportError = fail with the traceback as `actual`.

This catches:
  - Library's tests pass but the public surface is unusable (wrong types,
    missing __all__, broken re-exports)
  - The README's import example doesn't actually work
  - Methods exist but raise on the documented inputs
  - Module's __init__ has a side-effecting import that crashes on load
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass

from .types import Probe, ProbeFailure, VerificationResult


# ── Data shape ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UsageExample(Probe):
    """One snippet that imports from the library and asserts on its output."""
    language: str = "python"     # "python" | "node" | "rust"
    code: str = ""               # runnable snippet
    timeout_s: int = 15


# ── LLM-driven generation ────────────────────────────────────────────────────


_GENERATE_SYSTEM_PROMPT = """You are a senior QA engineer writing usage \
examples for a library. Given the spec stories and the public API surface, \
output runnable snippets that import from the library and assert behavior.

Output JSON only — no prose, no fences. Schema:

{
  "examples": [
    {
      "story_id": "S02",
      "title": "user passes input through the model",
      "priority": "must",
      "language": "python",
      "code": "import torch\\nfrom model import Net\\nn = Net()\\nout = n.forward(torch.randn(1, 3, 32, 32))\\nassert out.shape == (1, 10), f'expected (1,10), got {out.shape}'",
      "timeout_s": 15
    }
  ]
}

Rules:
- One example per must- and should-priority story. Skip "could" stories.
- The snippet runs from the workspace root (your library's package is \
importable as a top-level package).
- ALWAYS end with at least one `assert` that proves the story's acceptance \
criterion. A snippet that imports and runs without asserting only catches \
crash bugs, not semantic ones.
- For Python: import from the package, run, assert. Use `pytest`-free \
plain `assert`.
- For Node: use ESM imports if the package is "type": "module", CommonJS \
`require` otherwise. End with `process.exit(0)` after assertions pass.
- For Rust: use `cargo run --example <name>` style; the snippet body goes \
into the example file (begins with `fn main() { ... }`).
- Keep snippets ≤30 lines. Don't reimplement features in the snippet; \
import them.
- DO NOT reach into private names (anything prefixed with `_`).
- 5-12 examples for a typical library. Don't pad.

Output ONLY the JSON object."""


def _build_generate_prompt(spec, workspace: str, lang) -> str:
    name = getattr(lang, "name", "") if lang else ""
    family = getattr(lang, "family", "") if lang else ""
    surface = _summarize_public_surface(workspace, family)
    spec_block = spec.to_prompt_block(max_chars=3500) if spec else ""
    return (
        f"Language: {name}\n\n"
        f"PUBLIC API SURFACE:\n{surface}\n\n"
        f"{spec_block}\n\nGenerate examples."
    )


def _summarize_public_surface(workspace: str, family: str) -> str:
    """Read public re-exports / pub items from the project's lib root."""
    lines: list[str] = []
    if family == "python":
        for entry in sorted(os.listdir(workspace)):
            init = os.path.join(workspace, entry, "__init__.py")
            if not os.path.isfile(init):
                continue
            try:
                with open(init) as f:
                    text = f.read()
            except OSError:
                continue
            lines.append(f"package: {entry}")
            for ln in text.splitlines():
                stripped = ln.strip()
                if stripped.startswith("from .") or stripped.startswith("__all__"):
                    lines.append(f"  {stripped[:150]}")
    elif family == "node":
        for path_rel in ("index.ts", "src/index.ts", "index.js", "src/index.js"):
            full = os.path.join(workspace, path_rel)
            if os.path.isfile(full):
                try:
                    with open(full) as f:
                        text = f.read()
                except OSError:
                    continue
                lines.append(f"entry: {path_rel}")
                for ln in text.splitlines()[:40]:
                    s = ln.strip()
                    if s.startswith("export "):
                        lines.append(f"  {s[:150]}")
                break
    elif family == "compiled":
        # Rust lib: src/lib.rs `pub` items
        lib = os.path.join(workspace, "src", "lib.rs")
        if os.path.isfile(lib):
            try:
                with open(lib) as f:
                    text = f.read()
            except OSError:
                text = ""
            lines.append("entry: src/lib.rs")
            for ln in text.splitlines()[:80]:
                s = ln.strip()
                if s.startswith("pub "):
                    lines.append(f"  {s[:150]}")
    return "\n".join(lines) or "(no exports detected — heuristic missed)"


def generate_examples(spec, workspace: str, lang, cfg, emit) -> list[UsageExample]:
    from cadillac.engine import chat, extract_json

    if spec is None or spec.is_empty():
        return []
    messages = [
        {"role": "system", "content": _GENERATE_SYSTEM_PROMPT},
        {"role": "user", "content": _build_generate_prompt(spec, workspace, lang)},
    ]
    emit("log", msg="[RUNTIME/lib] generating usage examples...")
    try:
        msg = chat(cfg, messages, tools=[], emit=emit)
    except Exception as e:
        emit("log", msg=f"[RUNTIME/lib] LLM error: {e}")
        return []
    raw = (msg.get("content") or "").strip()
    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        return []
    raw_examples = parsed.get("examples") or []
    if not isinstance(raw_examples, list):
        return []
    examples: list[UsageExample] = []
    for entry in raw_examples:
        if not isinstance(entry, dict):
            continue
        ex = _example_from_dict(entry)
        if ex is not None:
            examples.append(ex)

    try:
        cad_dir = os.path.join(workspace, ".cadillac")
        os.makedirs(cad_dir, exist_ok=True)
        with open(os.path.join(cad_dir, "usage_examples.json"), "w") as f:
            json.dump([_example_to_dict(e) for e in examples], f, indent=2)
    except OSError:
        pass

    emit("log", msg=f"[RUNTIME/lib] {len(examples)} example(s) generated")
    return examples


def _example_from_dict(d: dict) -> UsageExample | None:
    try:
        story_id = str(d.get("story_id", "")).strip()
        title = str(d.get("title", "")).strip()
        priority = str(d.get("priority", "must")).strip()
        if priority not in ("must", "should", "could"):
            priority = "must"
        language = str(d.get("language", "python")).strip()
        if language not in ("python", "node", "rust"):
            return None
        code = str(d.get("code", "")).strip()
        if not story_id or not code:
            return None
        return UsageExample(
            story_id=story_id, title=title, priority=priority,
            language=language, code=code,
            timeout_s=int(d.get("timeout_s", 15) or 15),
        )
    except (TypeError, ValueError):
        return None


def _example_to_dict(e: UsageExample) -> dict:
    return {
        "story_id": e.story_id, "title": e.title, "priority": e.priority,
        "language": e.language, "code": e.code, "timeout_s": e.timeout_s,
    }


# ── Execution ────────────────────────────────────────────────────────────────


_EXT = {"python": ".py", "node": ".mjs", "rust": ".rs"}


def _interpreter(language: str) -> list[str] | None:
    if language == "python":
        return ["python3"]
    if language == "node":
        return ["node"]
    return None  # rust handled separately


def _run_python_or_node(example: UsageExample, workspace: str) -> tuple[int, str, str]:
    """Write snippet to a temp file inside workspace (so imports resolve), execute.

    Returns (exit_code, stdout, stderr).
    """
    interp = _interpreter(example.language)
    if interp is None:
        return (1, "", f"unsupported language: {example.language}")
    fd, path = tempfile.mkstemp(
        prefix=f"_runtime_lib_{example.story_id}_",
        suffix=_EXT[example.language], dir=workspace,
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(example.code)
        try:
            r = subprocess.run(
                interp + [path], cwd=workspace,
                capture_output=True, text=True,
                timeout=example.timeout_s,
            )
            return (r.returncode, r.stdout, r.stderr)
        except subprocess.TimeoutExpired:
            return (124, "", f"snippet timed out after {example.timeout_s}s")
        except FileNotFoundError as e:
            return (127, "", f"interpreter not found: {e}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _run_rust(example: UsageExample, workspace: str) -> tuple[int, str, str]:
    """Write snippet to `examples/<id>.rs`, run via `cargo run --example`."""
    examples_dir = os.path.join(workspace, "examples")
    os.makedirs(examples_dir, exist_ok=True)
    example_name = f"runtime_verify_{example.story_id}".replace(".", "_").replace("-", "_")
    rs_path = os.path.join(examples_dir, f"{example_name}.rs")
    try:
        with open(rs_path, "w") as f:
            f.write(example.code)
        try:
            r = subprocess.run(
                ["cargo", "run", "--quiet", "--example", example_name],
                cwd=workspace, capture_output=True, text=True,
                timeout=example.timeout_s,
            )
            return (r.returncode, r.stdout, r.stderr)
        except subprocess.TimeoutExpired:
            return (124, "", f"cargo run timed out after {example.timeout_s}s")
        except FileNotFoundError:
            return (127, "", "cargo not on PATH")
    finally:
        try:
            os.unlink(rs_path)
        except OSError:
            pass


def run_example(example: UsageExample, workspace: str) -> ProbeFailure | None:
    if example.language == "rust":
        code, _stdout, stderr = _run_rust(example, workspace)
    else:
        code, _stdout, stderr = _run_python_or_node(example, workspace)
    if code == 0:
        return None
    actual = (stderr or "").strip() or f"non-zero exit ({code})"
    if "ModuleNotFoundError" in actual or "ImportError" in actual:
        kind = "import_error"
    elif code == 124:
        kind = "timeout"
    elif "AssertionError" in actual or "assertion failed" in actual:
        kind = "assertion"
    else:
        kind = "exit_code_mismatch"
    return ProbeFailure(
        probe=example,
        failure_kind=kind,
        detail=f"snippet exited {code}",
        actual=actual[-800:],
    )


# ── Strategy entry point ─────────────────────────────────────────────────────


def run(spec, workspace: str, lang, cfg, contract, emit) -> VerificationResult:
    examples = generate_examples(spec, workspace, lang, cfg, emit)
    if not examples:
        return VerificationResult(strategy="library", probes_run=0,
                                   skipped_reason="no examples generated")
    failures: list[ProbeFailure] = []
    for ex in examples:
        emit("log", msg=f"[RUNTIME/lib] {ex.story_id}: {ex.title}")
        fail = run_example(ex, workspace)
        if fail is not None:
            emit("log", msg=f"  [fail] {fail.failure_kind} — {fail.detail}")
            failures.append(fail)
    return VerificationResult(
        strategy="library", probes_run=len(examples), failures=tuple(failures),
    )
