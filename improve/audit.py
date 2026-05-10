"""LLM-driven self-audit of the cadillac source tree.

Reads the cadillac/ package (compressed via codemap), asks the LLM to
identify weaknesses with severity tags, returns ranked JSON. The result
gets correlated against probe failures in the next stage.

Self-modification guard: the audit explicitly refuses to flag weaknesses
in `cadillac/improve/*.py` so the loop can't recursively rewrite itself
into a confirmation-bias attractor.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict


_AUDIT_PROMPT = """\
You are auditing the source code of an LLM-driven autobuilder. Your job is
to identify real weaknesses — bugs, race conditions, silent failure paths,
missing validation, prompt drift, resource leaks — that would degrade build
quality or cause silent failures.

Return ONLY a JSON array. Each entry:
{{
  "weakness_id": "short-snake_case-id",
  "file": "cadillac/<file>.py",
  "lines": "approx line range like '123-145'",
  "severity": "high" | "medium" | "low",
  "what": "one-sentence description of the bug",
  "why": "one-sentence explanation of how this manifests in builds",
  "fix_sketch": "one-sentence description of a concrete fix"
}}

CONSTRAINTS:
- Do NOT flag anything in cadillac/improve/ — that's the loop running this audit.
- Cap at 12 entries. Quality over quantity.
- "high" severity ONLY if you'd expect it to break a real build or corrupt state.
- "medium" if it's a real issue but bounded blast radius (warnings, occasional retries).
- "low" if it's hygiene or edge-case.

Return a JSON array. No commentary, no markdown fences, just the array.

═══════════════════════════════════════════════════════════════════════════
SOURCE TREE
═══════════════════════════════════════════════════════════════════════════

{source_dump}
"""


@dataclass
class AuditFinding:
    weakness_id: str
    file: str
    lines: str
    severity: str  # high | medium | low
    what: str
    why: str
    fix_sketch: str

    def to_dict(self) -> dict:
        return asdict(self)


def _gather_source(workspace: str, *, max_chars: int = 80_000) -> str:
    """Concatenate cadillac/ source into a single block, capped at max_chars.

    Skips the improve/ subpackage (self-modification guard) and tests/.
    Within the cap, prefers files in core build-flow order.
    """
    src_dir = os.path.join(workspace, "cadillac")
    if not os.path.isdir(src_dir):
        return ""

    # Priority order — read these first so we don't run out of budget on
    # peripheral files
    priority_files = [
        "engine.py", "phases.py", "modules.py", "validate.py",
        "tools.py", "manifest.py", "scratch.py", "memory.py",
        "progress.py", "languages.py", "contracts.py", "topology.py",
        "adversarial.py", "inspector.py", "codemap.py", "_atomic.py",
    ]

    parts: list[str] = []
    used = 0
    for fname in priority_files:
        path = os.path.join(src_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                content = f.read()
        except OSError:
            continue
        rel = f"cadillac/{fname}"
        budget_left = max_chars - used
        if budget_left < 1000:
            break
        if len(content) > budget_left:
            content = content[:budget_left] + "\n# ... (truncated)\n"
        parts.append(f"### {rel}\n```python\n{content}\n```\n")
        used += len(content)

    return "\n".join(parts)


def _strip_fences(raw: str) -> str:
    """LLM may wrap output in ```json ... ``` despite being told not to."""
    raw = raw.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", raw, re.DOTALL)
    return m.group(1).strip() if m else raw


def _filter_self_modifications(findings: list[dict]) -> list[dict]:
    """Drop any finding pointing at cadillac/improve/* — self-modification guard."""
    return [
        f for f in findings
        if not (f.get("file", "").startswith("cadillac/improve/")
                or "improve/" in f.get("file", ""))
    ]


def run_audit(cadillac_root: str, cfg, emit) -> list[AuditFinding]:
    """Run one audit pass. cadillac_root should be the dir containing cadillac/."""
    from ..engine import chat

    source_dump = _gather_source(cadillac_root)
    if not source_dump:
        emit("log", msg="[improve/audit] no source found, skipping")
        return []

    prompt = _AUDIT_PROMPT.format(source_dump=source_dump)
    emit("log", msg="[improve/audit] querying LLM...")
    try:
        msg = chat(cfg, [{"role": "user", "content": prompt}], tools=[], emit=emit)
    except Exception as e:
        emit("log", msg=f"[improve/audit] LLM call failed: {e}")
        return []

    raw = (msg.get("content") or "").strip()
    text = _strip_fences(raw)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        emit("log", msg=f"[improve/audit] LLM returned invalid JSON: {e}")
        return []

    if not isinstance(data, list):
        emit("log", msg="[improve/audit] LLM returned non-list response")
        return []

    data = _filter_self_modifications(data)

    findings: list[AuditFinding] = []
    for entry in data[:12]:  # cap defensively
        if not isinstance(entry, dict):
            continue
        try:
            findings.append(AuditFinding(
                weakness_id=str(entry.get("weakness_id", "?"))[:80],
                file=str(entry.get("file", ""))[:200],
                lines=str(entry.get("lines", ""))[:30],
                severity=str(entry.get("severity", "low")).lower(),
                what=str(entry.get("what", ""))[:300],
                why=str(entry.get("why", ""))[:300],
                fix_sketch=str(entry.get("fix_sketch", ""))[:300],
            ))
        except Exception:
            continue

    emit("log", msg=f"[improve/audit] {len(findings)} weakness(es) identified")
    return findings


def save_findings(findings: list[AuditFinding], path: str) -> None:
    from .._atomic import atomic_write_text
    text = json.dumps([f.to_dict() for f in findings], indent=2)
    atomic_write_text(path, text)
