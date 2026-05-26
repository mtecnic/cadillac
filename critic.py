"""Completeness critic — runs after VALIDATE green, finds missing features.

Different from `cadillac/improve/proposer.py` (which proposes patches in a
self-improvement loop against an audit) and from `Phase.REVIEW` in engine.py
(which catches code-quality bugs before BUILD). This critic exists for one
reason: VALIDATE passing means "what's built works", not "what was specified
got built". Spec gives us the bar; the critic measures the gap.

Two stages:

  1. STATIC PRE-FILTER (cheap)
     Run spec.coverage_report() to flag stories whose keywords never appear
     in the codebase. These are almost-certainly missing — no point asking
     the LLM to confirm. They become MissingFeature entries directly.

  2. LLM SECOND OPINION (expensive)
     For ambiguous cases (story keywords ARE present but maybe only in
     unrelated context), send the spec + tier-2 codemap to the LLM with one
     question: "for each story, does the code implement it? if not, why and
     where would it go?"

The orchestrator (`audit_completeness`) returns a ranked list. The engine
uses the top N to drive ONE iterate pass — capped, so a confused critic
can't loop forever.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .spec import Spec, Story, coverage_report


_MUST_WEIGHT = 3.0
_SHOULD_WEIGHT = 1.0
_COULD_WEIGHT = 0.3


@dataclass(frozen=True)
class MissingFeature:
    """One story the critic believes is unimplemented.

    `severity` is derived from the story's priority: must → high,
    should → medium, could → low. `confidence` is 1.0 for static-prefilter
    misses (we KNOW the keywords aren't in the codebase) and lower for
    LLM-flagged misses (the LLM might be wrong).
    """
    story_id: str
    title: str
    priority: str
    severity: str           # "high" | "medium" | "low"
    confidence: float       # 0.0–1.0
    why_missing: str        # one-line explanation
    suggested_files: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["suggested_files"] = list(self.suggested_files)
        return d


def _severity_for(priority: str) -> str:
    return {"must": "high", "should": "medium", "could": "low"}.get(
        priority, "medium"
    )


def _weight_for(priority: str) -> float:
    return {"must": _MUST_WEIGHT, "should": _SHOULD_WEIGHT,
            "could": _COULD_WEIGHT}.get(priority, _SHOULD_WEIGHT)


# ── LLM-driven second opinion ────────────────────────────────────────────────


_CRITIC_SYSTEM_PROMPT = """You are a senior engineer doing one job: given a \
user-story spec and a codebase summary, identify which stories the code does \
NOT implement.

You are NOT reviewing code quality. You are NOT looking for bugs. You only \
care about completeness: was each story built?

For each story flagged as POTENTIALLY missing, judge:
  - "missing": no implementation present
  - "partial": something is there but a core acceptance criterion is unmet
  - "covered": fully implemented (do NOT include these in output)

Output a JSON object — no prose, no fences:

{
  "missing": [
    {
      "story_id": "S07",
      "status": "missing",
      "why": "no /logout route in any backend file",
      "suggested_files": ["backend/routes/auth.py", "frontend/src/components/LogoutButton.tsx"],
      "confidence": 0.9
    }
  ]
}

Rules:
- ONLY include stories where status is "missing" or "partial". Skip "covered".
- `why` is one short sentence pointing at the gap, not the symptom.
- `suggested_files` lists 1-3 file paths where the implementation should go. \
Reuse existing project structure conventions visible in the codebase summary.
- `confidence` is 0.5–1.0. Use 0.9+ only when you're certain (no possible \
file you haven't seen could change the answer).
- DO NOT invent stories not in the spec. DO NOT renumber IDs.

Output ONLY the JSON object."""


def _build_critic_prompt(
    spec: Spec, code_map: str, candidate_story_ids: list[str],
    manifest_summary: str,
) -> str:
    """Render the user message for the critic LLM call.

    `candidate_story_ids` are the stories the static prefilter already ruled
    out — the LLM only adjudicates the ambiguous remainder, so the prompt
    stays small even for big specs.
    """
    by_id = {s.id: s for s in spec.stories}
    spec_lines = ["## SPEC (stories to check):"]
    for sid in candidate_story_ids:
        st = by_id.get(sid)
        if not st:
            continue
        spec_lines.append(
            f"\n### {st.id} [{st.priority}] {st.title}"
        )
        if st.category:
            spec_lines.append(f"  category: {st.category}")
        for a in st.acceptance:
            spec_lines.append(f"  - {a}")
    spec_block = "\n".join(spec_lines)

    return (
        f"{spec_block}\n\n"
        f"## CODEBASE SUMMARY:\n{code_map}\n\n"
        f"## FILE MANIFEST:\n{manifest_summary}\n\n"
        "For each spec story above, decide if the codebase implements it. "
        "Output JSON per the schema. Skip anything fully covered."
    )


def _llm_review_ambiguous(
    spec: Spec, ambiguous_story_ids: list[str],
    workspace: str, lang, cfg, manifest_summary: str, emit,
) -> list[MissingFeature]:
    """Ask the LLM about stories the static prefilter wasn't sure about."""
    if not ambiguous_story_ids:
        return []
    from .codemap import CodeMapBuilder
    from .engine import chat, extract_json
    budget = max(getattr(cfg, "max_context_tokens", 40000) - 10000, 10000)
    code_map = CodeMapBuilder(workspace, budget_tokens=budget,
                              lang=lang).build()
    user_msg = _build_critic_prompt(
        spec, code_map, ambiguous_story_ids, manifest_summary,
    )
    messages = [
        {"role": "system", "content": _CRITIC_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    emit("log", msg=f"[CRITIC] LLM reviewing {len(ambiguous_story_ids)} "
                    f"ambiguous stor{'y' if len(ambiguous_story_ids) == 1 else 'ies'}...")
    try:
        msg = chat(cfg, messages, tools=[], emit=emit)
    except Exception as e:
        emit("log", msg=f"[CRITIC] LLM error: {e}; skipping LLM pass")
        return []
    raw = (msg.get("content") or "").strip()
    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        emit("log", msg="[CRITIC] LLM did not return JSON object; skipping")
        return []
    raw_missing = parsed.get("missing") or []
    if not isinstance(raw_missing, list):
        return []

    by_id = {s.id: s for s in spec.stories}
    out: list[MissingFeature] = []
    for entry in raw_missing:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("story_id", "")).strip()
        st = by_id.get(sid)
        if not st:
            continue  # ignore hallucinated story IDs
        status = str(entry.get("status", "missing")).lower()
        if status not in ("missing", "partial"):
            continue
        try:
            conf = float(entry.get("confidence", 0.7))
        except (TypeError, ValueError):
            conf = 0.7
        conf = max(0.0, min(conf, 1.0))
        why = str(entry.get("why", "")).strip()[:300] or "no implementation found"
        files_raw = entry.get("suggested_files") or []
        if isinstance(files_raw, str):
            files_raw = [files_raw]
        files = tuple(str(f).strip() for f in files_raw if str(f).strip())[:3]
        out.append(MissingFeature(
            story_id=sid, title=st.title, priority=st.priority,
            severity=_severity_for(st.priority), confidence=conf,
            why_missing=why, suggested_files=files,
        ))
    return out


# ── Orchestrator ──────────────────────────────────────────────────────────────


def audit_completeness(
    spec: Spec, workspace: str, manifest_summary: str, lang, cfg,
    emit=None, *, llm_review: bool = True,
) -> list[MissingFeature]:
    """Identify stories the codebase doesn't implement.

    Two passes:
      1. Static keyword prefilter. Stories whose keywords are wholly absent
         from the workspace are flagged with high confidence (1.0).
      2. LLM second opinion on the remaining ambiguous stories — those that
         passed the prefilter (keywords present) but may still be unimplemented
         (keywords found in unrelated context).

    Set `llm_review=False` to skip stage 2 (tests use this).

    Returns missing features sorted by (severity desc, confidence desc).
    Caller decides how many to act on — typically the top 5-10.
    """
    if emit is None:
        emit = lambda *a, **kw: None
    if spec.is_empty():
        emit("log", msg="[CRITIC] empty spec, skipping")
        return []

    by_id = {s.id: s for s in spec.stories}
    static_report = coverage_report(spec, workspace)
    static_missing_ids: list[str] = list(static_report.get("missing", []))
    static_covered_ids: list[str] = list(static_report.get("covered", []))

    findings: list[MissingFeature] = []
    # Stage 1: static prefilter misses — confidence 1.0
    for sid in static_missing_ids:
        st = by_id.get(sid)
        if not st:
            continue
        findings.append(MissingFeature(
            story_id=st.id, title=st.title, priority=st.priority,
            severity=_severity_for(st.priority), confidence=1.0,
            why_missing="static scan: story keywords absent from codebase",
        ))
    if findings:
        emit("log", msg=f"[CRITIC] static prefilter flagged {len(findings)} "
                        "stor(y/ies) as missing")

    # Stage 2: LLM second opinion on the ambiguous ones
    if llm_review and static_covered_ids:
        llm_findings = _llm_review_ambiguous(
            spec, static_covered_ids, workspace, lang, cfg,
            manifest_summary, emit,
        )
        # Don't double-flag stories the static pass already caught.
        already = {f.story_id for f in findings}
        for f in llm_findings:
            if f.story_id not in already:
                findings.append(f)
        if llm_findings:
            emit("log", msg=f"[CRITIC] LLM flagged {len(llm_findings)} "
                            "additional gap(s)")

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (severity_rank.get(f.severity, 99),
                                  -f.confidence, f.story_id))
    return findings


# ── Score + rendering ─────────────────────────────────────────────────────────


def completeness_score(spec: Spec, missing: list[MissingFeature]) -> float:
    """Weighted score in [0, 1]. 1.0 = nothing missing; 0 = every must is missing."""
    if spec.is_empty():
        return 1.0
    total = sum(_weight_for(s.priority) for s in spec.stories) or 1.0
    miss_w = sum(_weight_for(m.priority) * m.confidence for m in missing)
    return max(0.0, min(1.0, 1.0 - miss_w / total))


def format_for_iterate(missing: list[MissingFeature], top_n: int = 8) -> str:
    """Render the top-N missing features as an iterate-phase instruction.

    Caps at `top_n` so the prompt stays digestible. Caller can call repeatedly
    if more remain after the first pass.
    """
    if not missing:
        return ""
    head = missing[:top_n]
    lines = [
        "The build's tests pass, but the following user stories from the "
        "spec are not implemented. Add them now. Use the suggested files as "
        "a starting point; reuse existing patterns where possible.",
        "",
    ]
    for m in head:
        lines.append(f"### {m.story_id} [{m.priority}] {m.title}")
        lines.append(f"  why missing: {m.why_missing}")
        if m.suggested_files:
            lines.append("  suggested files: " + ", ".join(m.suggested_files))
        lines.append("")
    if len(missing) > top_n:
        lines.append(f"(+ {len(missing) - top_n} more stories deferred to a "
                     "later pass — focus on the above first.)")
    return "\n".join(lines)
