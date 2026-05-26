"""Surgical-fix mode — narrow one-shot edits when the build is stuck.

Triggered from `engine.py` when the same validation failure fingerprint
repeats across 3 consecutive VALIDATE retries. The repeat is the signal
that the regular BUILD-iterate loop isn't making progress — same input
context, same wrong output, every time.

The surgical pass changes strategy:
  - Much smaller context: just the failing file's 20-line window around
    the reported line, no codemap, no lessons, no scratch.
  - Single-purpose prompt: "Fix ONLY this error. Output a minimal patch."
  - Apply, re-run only the failing check, return True iff cleared.

Cap: a fingerprint may trigger surgical mode at most once per build. If it
doesn't clear, log and continue — we don't surgical-on-surgical.

Out of scope: project-wide errors (`node_modules missing`, "package.json
not found") — those don't pin to a file:line and can't be addressed by a
single-file edit. The parser returns nothing for those, the engine skips
surgical mode, and they fall through to the existing retry path.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


# ── Error parsing ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StaticError:
    """One actionable validation error pinned to a specific source location.

    The fingerprint is stable across retries (file + line + check_name +
    error_class), so the same bug reported on retry 1 and retry 4 produces
    the same string. That stability is what makes "same fingerprint 3 times"
    a useful stuck-loop signal.
    """
    check_name: str   # "static_names" | "syntax" | "lint" | "imports"
    file: str         # workspace-relative path
    line: int
    col: int          # 0 when not reported
    error_class: str  # "undefined name" | "syntax error" | "F821" | ...
    detail: str       # raw diagnostic line, used in surgical prompt

    @property
    def fingerprint(self) -> str:
        return f"{self.check_name}:{self.file}:{self.line}:{self.error_class}"


# pyflakes format used by check_static_names:
#   path:line:col: undefined name 'X'
#   path:line:col: 'X' may be undefined, or defined from star imports
_PYFLAKES_RE = re.compile(
    r"^(?P<file>[^\s:]+\.py):(?P<line>\d+):(?P<col>\d+):\s+(?P<msg>.+)$"
)

# py_compile / SyntaxError format used by check_syntax:
#   path:line: error
# or:
#   File "path", line N
_SYNTAX_RE = re.compile(
    r"^(?P<file>[^\s:]+\.py):(?P<line>\d+):\s+(?P<msg>.+)$"
)

# ruff format used by check_lint:
#   path:line:col: CODE error message
_RUFF_RE = re.compile(
    r"^(?P<file>[^\s:]+\.py):(?P<line>\d+):(?P<col>\d+):\s+(?P<code>[A-Z]\d+)\s+(?P<msg>.+)$"
)


def _classify_pyflakes(msg: str) -> str:
    """Bucket the pyflakes message into a stable error_class for fingerprinting."""
    if "undefined name" in msg:
        return "undefined name"
    if "may be undefined" in msg:
        return "may be undefined"
    if "syntax error" in msg.lower():
        return "syntax error"
    if "imported but unused" in msg:
        return "unused import"
    return msg.split(":", 1)[0].strip()[:40]


def parse_static_errors(results: list) -> list[StaticError]:
    """Walk CheckResult objects, pull file:line errors out of failed checks.

    Returns one StaticError per actionable diagnostic. Skips project-wide
    failures (no file:line) and skips passed/warning checks.
    """
    errors: list[StaticError] = []
    for r in results:
        if r.passed:
            continue
        if r.severity != "error":
            continue
        name = r.name
        if name not in ("static_names", "syntax", "lint", "imports"):
            continue
        text = r.output or ""
        for line in text.splitlines():
            line = line.rstrip()
            if not line:
                continue
            # Try parsers in order of specificity
            m = _PYFLAKES_RE.match(line.strip())
            if m and name in ("static_names", "imports"):
                file_path = m.group("file").lstrip("./")
                errors.append(StaticError(
                    check_name=name,
                    file=file_path,
                    line=int(m.group("line")),
                    col=int(m.group("col")),
                    error_class=_classify_pyflakes(m.group("msg")),
                    detail=line.strip(),
                ))
                continue
            m = _RUFF_RE.match(line.strip())
            if m and name == "lint":
                file_path = m.group("file").lstrip("./")
                errors.append(StaticError(
                    check_name=name,
                    file=file_path,
                    line=int(m.group("line")),
                    col=int(m.group("col")),
                    error_class=m.group("code"),
                    detail=line.strip(),
                ))
                continue
            m = _SYNTAX_RE.match(line.strip())
            if m and name == "syntax":
                file_path = m.group("file").lstrip("./")
                errors.append(StaticError(
                    check_name=name,
                    file=file_path,
                    line=int(m.group("line")),
                    col=0,
                    error_class="syntax error",
                    detail=line.strip(),
                ))
    return errors


# ── Surgical fix ─────────────────────────────────────────────────────────────


_SURGICAL_SYSTEM_PROMPT = """You are a senior engineer doing one job: \
fixing one specific error with the minimum possible edit. The build's \
general iterate loop has tried 3+ times to fix this and keeps failing. \
You see ONLY the failing file and the error. No codemap, no spec, no other \
context. Focus.

You will be given:
  - The error (file:line:col + error class + raw diagnostic)
  - A 20-line window of the source file around the error
  - For "undefined name" errors: the file's existing imports + a list of \
candidate definitions found elsewhere in the workspace + sibling-package \
__init__.py exports. Use that to pick the right import.

Output JSON only — no prose, no fences:

{
  "old_string": "<the exact substring from the file that needs to change>",
  "new_string": "<the replacement>",
  "explanation": "<one short sentence about what was wrong>"
}

Rules:
- `old_string` MUST be an exact, unique substring of the file as shown. \
Include enough surrounding whitespace and context that it's unambiguous. \
Typically 1-5 lines. When ADDING an import, set `old_string` to an existing \
import line (so the new import slots beside it) and put both the existing \
and new lines in `new_string`.
- `new_string` is what `old_string` becomes. Keep the change as small as \
possible — the error is at a specific line, the fix should be local to \
that line.
- DO NOT rewrite the function. DO NOT add new methods. DO NOT change \
formatting beyond what the fix requires.
- For `undefined name 'X'`: first try to add an import for X based on the \
candidates list. If X is defined in `<some_module>` and the file is in the \
same package, use `from <some_module> import X` (or `from .<module> import X` \
for intra-package). Only rename X if there's no plausible definition and \
the local context shows the wrong name was used.

Output ONLY the JSON object."""


def _read_window(workspace: str, file_rel: str, line: int,
                 radius: int = 10) -> str:
    """Read ±radius lines around `line`. Returns formatted with line numbers."""
    path = os.path.join(workspace, file_rel)
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError as e:
        return f"<could not read {file_rel}: {e}>"
    start = max(0, line - 1 - radius)
    end = min(len(lines), line + radius)
    out = []
    for i in range(start, end):
        marker = ">>>" if (i + 1) == line else "   "
        out.append(f"{marker} {i + 1:4d} | {lines[i].rstrip()}")
    return "\n".join(out)


_UNDEFINED_NAME_RE = re.compile(r"undefined name '([^']+)'")


def _extract_undefined_name(detail: str) -> str | None:
    m = _UNDEFINED_NAME_RE.search(detail)
    return m.group(1) if m else None


def _file_imports(workspace: str, file_rel: str, limit: int = 30) -> str:
    """Return a short block of import lines from the failing file.

    Surgical's prompt budget is tight; cap at `limit` so a file with
    100 imports doesn't crowd out the other hints.
    """
    path = os.path.join(workspace, file_rel)
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return ""
    imports: list[str] = []
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith(("import ", "from ")):
            imports.append(ln.rstrip())
            if len(imports) >= limit:
                break
    return "\n".join(imports)


def _candidate_definitions(workspace: str, name: str,
                            max_hits: int = 8) -> list[tuple[str, str]]:
    """Find files that define `name` as a class, function, or constant.

    Returns up to `max_hits` `(rel_path, definition_line)` tuples. Walks
    the workspace, skipping the usual caches. Definition syntax:
      class Name
      def name
      Name = ...                       (uppercase / dunder)
      NAME = ...                       (constant)
    """
    if not name or not name.isidentifier():
        return []
    hits: list[tuple[str, str]] = []
    # Match either `class Name`, `def name`, or top-level `Name = `.
    pattern = re.compile(
        rf"^\s*(class\s+{re.escape(name)}\b"
        rf"|def\s+{re.escape(name)}\b"
        rf"|{re.escape(name)}\s*(?::\s*[^=]+)?\s*=)",
        re.MULTILINE,
    )
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in (
            "node_modules", ".git", "__pycache__", "dist", "build",
            "venv", ".venv", ".cadillac", "frontend",
        )]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path) as f:
                    text = f.read()
            except OSError:
                continue
            m = pattern.search(text)
            if not m:
                continue
            # Get just the matched line for display.
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            if line_end < 0:
                line_end = len(text)
            line = text[line_start:line_end].rstrip()
            rel = os.path.relpath(path, workspace)
            hits.append((rel, line.strip()))
            if len(hits) >= max_hits:
                return hits
    return hits


def _sibling_init_exports(workspace: str, file_rel: str) -> str:
    """Return the __init__.py contents for the package containing file_rel
    plus its sibling packages' __init__.py files. Useful when the LLM needs
    to know what names a sister package exposes.

    Capped at ~25 lines per __init__ to keep prompts small.
    """
    file_dir = os.path.dirname(file_rel)
    out: list[str] = []
    candidates: set[str] = set()
    # File's own package __init__
    if file_dir:
        candidates.add(os.path.join(file_dir, "__init__.py"))
    # Sibling packages at the project root
    try:
        for entry in os.listdir(workspace):
            full = os.path.join(workspace, entry)
            if os.path.isdir(full) and not entry.startswith((".", "__")):
                init = os.path.join(entry, "__init__.py")
                if os.path.isfile(os.path.join(workspace, init)):
                    candidates.add(init)
    except OSError:
        return ""

    for cand in sorted(candidates):
        full = os.path.join(workspace, cand)
        if not os.path.isfile(full):
            continue
        try:
            with open(full) as f:
                text = f.read()
        except OSError:
            continue
        # Keep only import/__all__ lines — the actual exports
        keep: list[str] = []
        for ln in text.splitlines()[:80]:
            s = ln.strip()
            if s.startswith(("from ", "import ", "__all__")):
                keep.append(ln.rstrip())
        if keep:
            out.append(f"# {cand}")
            out.extend(keep[:15])
            out.append("")
    return "\n".join(out).strip()


def _build_surgical_prompt(error: StaticError, window: str,
                            workspace: str | None = None) -> str:
    """Compose the user message for the surgical LLM call.

    For "undefined name" errors, appends three context blocks: the file's
    existing imports, candidate definitions of the missing name elsewhere
    in the workspace, and the relevant __init__.py exports. Surgical's
    earlier ceiling was hitting exactly this case (the LLM saw `User` was
    undefined but had no idea which file declared `class User`); these
    hints close that gap.
    """
    parts = [
        f"FILE: {error.file}",
        f"ERROR: {error.detail}",
        f"ERROR CLASS: {error.error_class}",
        f"CHECK: {error.check_name}",
        "",
        "## SOURCE WINDOW (line marked with `>>>`):",
        window,
    ]
    if (error.error_class == "undefined name"
            and workspace is not None):
        name = _extract_undefined_name(error.detail)
        if name:
            imports_block = _file_imports(workspace, error.file)
            if imports_block:
                parts += ["", f"## CURRENT IMPORTS in {error.file}:",
                          imports_block]
            candidates = _candidate_definitions(workspace, name)
            if candidates:
                cand_lines = [f"  {rel}: {line}" for rel, line in candidates]
                parts += ["", f"## CANDIDATE DEFINITIONS of '{name}':"]
                parts += cand_lines
            else:
                parts += ["",
                          f"## NO DEFINITIONS of '{name}' FOUND in the "
                          "workspace — this name probably needs to come "
                          "from a third-party library or be renamed."]
            siblings = _sibling_init_exports(workspace, error.file)
            if siblings:
                parts += ["", "## SIBLING PACKAGE EXPORTS:", siblings]
    parts += ["", "Fix the error with the smallest possible edit. "
              "JSON output only."]
    return "\n".join(parts)


def _apply_edit(workspace: str, file_rel: str,
                 old_string: str, new_string: str) -> tuple[bool, str]:
    """Replace `old_string` with `new_string` in the file. Returns (ok, reason)."""
    path = os.path.join(workspace, file_rel)
    if not os.path.isfile(path):
        return (False, f"file not found: {file_rel}")
    try:
        with open(path) as f:
            text = f.read()
    except OSError as e:
        return (False, f"read failed: {e}")
    count = text.count(old_string)
    if count == 0:
        return (False, "old_string not found in file")
    if count > 1:
        return (False, f"old_string is ambiguous (appears {count} times); LLM must include more context")
    new_text = text.replace(old_string, new_string, 1)
    try:
        with open(path, "w") as f:
            f.write(new_text)
    except OSError as e:
        return (False, f"write failed: {e}")
    return (True, "")


def _recheck(error: StaticError, workspace: str, lang) -> bool:
    """Re-run ONLY the failing check; return True iff the error fingerprint cleared.

    Cheaper than the full validation pipeline, and lets the engine bounce
    back to the retry loop with a sharper signal.
    """
    from .validate import (
        check_imports,
        check_lint,
        check_static_names,
        check_syntax,
    )
    runner = {
        "static_names": check_static_names,
        "syntax": check_syntax,
        "lint": check_lint,
        "imports": check_imports,
    }.get(error.check_name)
    if runner is None:
        return False
    try:
        results = runner(workspace, lang)
    except Exception:
        return False
    # Re-parse, check whether the same fingerprint survives
    fresh_errors = parse_static_errors(results)
    return error.fingerprint not in {e.fingerprint for e in fresh_errors}


def surgical_fix(error: StaticError, workspace: str, lang, cfg, emit) -> bool:
    """Run one targeted LLM call to fix `error`. Returns True iff cleared.

    Never raises. On LLM error, parse error, ambiguous edit, or non-clearing
    fix, returns False and logs why.
    """
    from .engine import chat, extract_json

    window = _read_window(workspace, error.file, error.line)
    if window.startswith("<could not read"):
        emit("log", msg=f"[SURGICAL] {error.fingerprint}: {window}")
        return False

    messages = [
        {"role": "system", "content": _SURGICAL_SYSTEM_PROMPT},
        {"role": "user", "content": _build_surgical_prompt(error, window, workspace)},
    ]
    emit("log", msg=f"[SURGICAL] {error.fingerprint} — asking for targeted fix...")
    try:
        msg = chat(cfg, messages, tools=[], emit=emit)
    except Exception as e:
        emit("log", msg=f"[SURGICAL] LLM error: {e}")
        return False
    raw = (msg.get("content") or "").strip()
    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        emit("log", msg="[SURGICAL] LLM did not return JSON")
        return False
    old_string = parsed.get("old_string")
    new_string = parsed.get("new_string")
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        emit("log", msg="[SURGICAL] response missing old_string/new_string")
        return False
    if old_string == new_string:
        emit("log", msg="[SURGICAL] old_string == new_string; no-op rejected")
        return False

    ok, reason = _apply_edit(workspace, error.file, old_string, new_string)
    if not ok:
        emit("log", msg=f"[SURGICAL] edit failed: {reason}")
        return False
    emit("log", msg=f"[SURGICAL] edit applied; re-checking {error.check_name}...")

    if _recheck(error, workspace, lang):
        emit("log", msg=f"[SURGICAL/CLEARED] {error.fingerprint}")
        return True
    emit("log", msg=f"[SURGICAL] fingerprint survived re-check; reverting expectation")
    # Note: we don't roll back the edit — the LLM may have made a partial
    # improvement, just not a complete fix. Caller decides what to do.
    return False
