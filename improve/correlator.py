"""Correlate probe failures to audit findings.

Given the audit's weakness list and the probe matrix's per-task outcomes
(failures, retry counts, error patterns), the LLM ranks which weaknesses
are most likely responsible for which observed failures and produces a
priority list for the proposer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict

from .audit import AuditFinding
from .scoring import IterationScore


_CORRELATE_PROMPT = """\
You are correlating bug reports against weaknesses in an autobuilder.

INPUT 1: ranked list of weaknesses found by static audit
{audit_block}

INPUT 2: per-task outcomes from running 8 reference workloads through the autobuilder
{probe_block}

YOUR JOB: for each FAILED or DEGRADED matrix task (passed_checks < total_checks
OR retry_rounds > 0 OR crashed=true), identify which audit weakness is the
LIKELY proximate cause. Return a JSON array of correlation candidates,
ordered by expected impact (highest first).

Each entry:
{{
  "weakness_id": "id from the audit list",
  "supporting_tasks": ["task_id_1", "task_id_2", ...],
  "expected_impact": "high" | "medium" | "low",
  "confidence": 0.0–1.0,
  "rationale": "one sentence linking the weakness to the failure mode"
}}

Cap at 5 candidates. Skip weaknesses you can't tie to a real probe outcome —
no speculative entries. Return a JSON array only, no markdown fences, no commentary.
"""


@dataclass
class CorrelationCandidate:
    weakness_id: str
    supporting_tasks: list[str]
    expected_impact: str  # high | medium | low
    confidence: float
    rationale: str

    def to_dict(self) -> dict:
        return asdict(self)


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", raw, re.DOTALL)
    return m.group(1).strip() if m else raw


def correlate(findings: list[AuditFinding],
              score: IterationScore,
              cfg, emit) -> list[CorrelationCandidate]:
    """LLM-driven mapping from probe outcomes to audit findings."""
    from ..engine import chat

    if not findings:
        emit("log", msg="[improve/correlate] no findings to correlate, skipping")
        return []

    # Show the LLM the audit list compactly
    audit_block = json.dumps(
        [f.to_dict() for f in findings], indent=2,
    )

    # Probe block is the per-task outcomes from the matrix
    probe_block = json.dumps(score.to_dict(), indent=2)

    prompt = _CORRELATE_PROMPT.format(
        audit_block=audit_block, probe_block=probe_block,
    )
    emit("log", msg="[improve/correlate] querying LLM...")
    try:
        msg = chat(cfg, [{"role": "user", "content": prompt}], tools=[], emit=emit)
    except Exception as e:
        emit("log", msg=f"[improve/correlate] LLM call failed: {e}")
        return []

    raw = (msg.get("content") or "").strip()
    text = _strip_fences(raw)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        emit("log", msg=f"[improve/correlate] invalid JSON: {e}")
        return []

    if not isinstance(data, list):
        return []

    valid_ids = {f.weakness_id for f in findings}
    candidates: list[CorrelationCandidate] = []
    for entry in data[:5]:
        if not isinstance(entry, dict):
            continue
        wid = str(entry.get("weakness_id", ""))
        if wid not in valid_ids:
            continue  # hallucinated id
        try:
            candidates.append(CorrelationCandidate(
                weakness_id=wid,
                supporting_tasks=[str(t)[:50] for t in
                                  (entry.get("supporting_tasks") or [])][:8],
                expected_impact=str(entry.get("expected_impact", "low")).lower(),
                confidence=max(0.0, min(1.0, float(entry.get("confidence", 0.5)))),
                rationale=str(entry.get("rationale", ""))[:300],
            ))
        except (TypeError, ValueError):
            continue

    emit("log", msg=f"[improve/correlate] {len(candidates)} candidate(s) ranked")
    return candidates
