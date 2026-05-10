"""Generate patches for the top-ranked weaknesses.

The proposer takes the audit finding + correlation rationale and asks the
LLM for a unified diff plus a regression test. The output goes to disk
where the applier picks it up.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict

from .audit import AuditFinding
from .correlator import CorrelationCandidate


_PROPOSE_PROMPT = """\
You are proposing a fix for a weakness in an autobuilder.

WEAKNESS:
- weakness_id: {weakness_id}
- file: {file}
- lines: {lines}
- severity: {severity}
- what: {what}
- why: {why}
- fix sketch: {fix_sketch}

CORRELATION RATIONALE (why this matters in real builds):
{rationale}

YOUR TASK: produce TWO outputs in this exact format:

═══ PATCH ═══
<a unified diff (git apply format) that fixes the weakness>
═══ TEST ═══
<a Python file content that pins the fix — the test should fail BEFORE the
patch and pass AFTER. Place it under tests/test_improve_<weakness_id>.py
and assume cadillac/ is importable as `cadillac.<module>`.>

CONSTRAINTS:
1. The PATCH MUST be a valid `git apply` unified diff with full headers
   (--- a/path, +++ b/path, @@ hunks). Use 3 lines of context.
2. The TEST must be self-contained: import what you need, run the assertions,
   no external setup. Standard unittest or pytest style is fine.
3. Do NOT modify cadillac/improve/* — that's the loop running this.
4. Do NOT modify cadillac/improve/matrix.py.
5. Do NOT modify any tests/test_improve_*.py file other than the one
   you're creating.
6. Keep the patch surgical — single concern, smallest change that fixes
   the bug. If the fix is too large, return EMPTY between the markers.

Output the markers literally. No commentary outside them.

CURRENT SOURCE OF THE TARGET FILE:
{source}
"""


@dataclass
class Proposal:
    weakness_id: str
    patch: str  # unified diff
    test_path: str
    test_content: str
    target_file: str

    def to_dict(self) -> dict:
        return asdict(self)


def _read_target(workspace: str, target_file: str) -> str:
    """Best-effort read of the file the patch targets, capped to keep prompt small."""
    path = os.path.join(workspace, target_file)
    if not os.path.isfile(path):
        return f"# (file {target_file} does not exist)"
    try:
        with open(path) as f:
            content = f.read(40_000)
        return content
    except OSError:
        return ""


def _split_response(raw: str) -> tuple[str, str]:
    """Pull (patch_text, test_text) from the LLM response."""
    # Tolerate variations: ═══ PATCH ═══, === PATCH ===, ## PATCH ##
    pat_marker = re.compile(r"^\s*[═=#]+\s*PATCH\s*[═=#]+\s*$", re.MULTILINE)
    test_marker = re.compile(r"^\s*[═=#]+\s*TEST\s*[═=#]+\s*$", re.MULTILINE)

    pat_match = pat_marker.search(raw)
    test_match = test_marker.search(raw)
    if not pat_match or not test_match:
        return "", ""

    patch = raw[pat_match.end():test_match.start()].strip()
    test = raw[test_match.end():].strip()

    # Strip trailing ``` fences if the LLM added them inside sections
    patch = re.sub(r"^```(?:diff|patch)?\s*\n", "", patch)
    patch = re.sub(r"\n```\s*$", "", patch)
    test = re.sub(r"^```(?:python|py)?\s*\n", "", test)
    test = re.sub(r"\n```\s*$", "", test)

    return patch.strip(), test.strip()


def _is_self_modifying(patch: str) -> bool:
    """Block any patch that touches the improve loop itself or its tests."""
    blocked_paths = (
        "cadillac/improve/",
        "tests/test_improve_round",  # audit rounds we shipped
    )
    for line in patch.splitlines():
        if line.startswith(("+++ ", "--- ", "diff --git ")):
            for blocked in blocked_paths:
                if blocked in line:
                    return True
    return False


def propose(finding: AuditFinding, candidate: CorrelationCandidate,
            workspace: str, cfg, emit) -> Proposal | None:
    """Ask the LLM for a patch + regression test for one finding."""
    from ..engine import chat

    source = _read_target(workspace, finding.file)
    prompt = _PROPOSE_PROMPT.format(
        weakness_id=finding.weakness_id,
        file=finding.file,
        lines=finding.lines,
        severity=finding.severity,
        what=finding.what,
        why=finding.why,
        fix_sketch=finding.fix_sketch,
        rationale=candidate.rationale,
        source=source,
    )

    emit("log", msg=f"[improve/propose] {finding.weakness_id}: querying LLM")
    try:
        msg = chat(cfg, [{"role": "user", "content": prompt}], tools=[], emit=emit)
    except Exception as e:
        emit("log", msg=f"[improve/propose] LLM call failed: {e}")
        return None

    raw = (msg.get("content") or "").strip()
    patch, test = _split_response(raw)

    if not patch or not test:
        emit("log", msg=f"[improve/propose] {finding.weakness_id}: "
                         "missing PATCH or TEST marker, skipping")
        return None

    if _is_self_modifying(patch):
        emit("log", msg=f"[improve/propose] {finding.weakness_id}: "
                         "patch attempts to modify the improve loop itself, REJECTED")
        return None

    test_path = f"tests/test_improve_{finding.weakness_id}.py"

    return Proposal(
        weakness_id=finding.weakness_id,
        patch=patch,
        test_path=test_path,
        test_content=test,
        target_file=finding.file,
    )


def save_proposal(proposal: Proposal, dir_path: str) -> tuple[str, str]:
    """Write the patch and test files to disk. Returns (patch_path, test_path)."""
    from .._atomic import atomic_write_text
    os.makedirs(dir_path, exist_ok=True)
    patch_path = os.path.join(dir_path, f"{proposal.weakness_id}.patch")
    test_path = os.path.join(dir_path, f"{proposal.weakness_id}.test.py")
    atomic_write_text(patch_path, proposal.patch + "\n")
    atomic_write_text(test_path, proposal.test_content + "\n")
    return patch_path, test_path
