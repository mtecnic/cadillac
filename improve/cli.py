"""Top-level orchestrator for `cadillac improve`.

Drives one full cycle per iteration:
  audit → probe → correlate → propose → apply+verify → record.

Exits when:
  - Matrix saturated for K=3 consecutive iterations
  - K=3 consecutive iterations apply zero patches (diminishing returns)
  - max-iterations cap (default 30)
  - improve/STOP file appears
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from .audit import run_audit, save_findings, AuditFinding
from .applier import apply_proposal, commit_applied
from .correlator import correlate, CorrelationCandidate
from .log import ImproveLog
from .matrix import MATRIX, run_matrix_task
from .proposer import propose, save_proposal
from .scoring import IterationScore, TaskOutcome, is_improvement, is_saturated


_STOP_NEEDED = 3  # consecutive saturated / zero-patch iterations to terminate
_DEFAULT_MAX_ITER = 30


@dataclass
class CycleStop:
    reason: str  # "saturated" | "diminishing_returns" | "iteration_cap" | "manual" | "error"
    iterations: int


def _check_stop_file(cadillac_root: str) -> bool:
    return os.path.isfile(os.path.join(cadillac_root, "improve", "STOP"))


def _probe_matrix(cfg, cadillac_root: str, iteration: int, emit) -> IterationScore:
    """Run every task in the matrix and return the score."""
    outcomes: list[TaskOutcome] = []
    base_ws = os.path.join(cadillac_root, "improve", "matrix-runs",
                           f"iter-{iteration}")
    os.makedirs(base_ws, exist_ok=True)
    for task in MATRIX:
        emit("log", msg=f"[improve/probe] running {task.id}...")
        t0 = time.time()
        try:
            outcome = run_matrix_task(task, cfg, base_workspace_dir=base_ws,
                                       iteration=iteration)
        except Exception as e:
            outcome = TaskOutcome(
                task_id=task.id, passed_checks=0,
                total_checks=len(task.expected_validation_keys),
                retry_rounds=0, elapsed_s=time.time() - t0,
                crashed=True, error_summary=f"runner crash: {e}",
            )
        outcomes.append(outcome)
        emit("log", msg=(
            f"[improve/probe] {task.id}: {outcome.passed_checks}/"
            f"{outcome.total_checks} passed, {outcome.retry_rounds} retries, "
            f"{outcome.elapsed_s:.0f}s"
        ))
    return IterationScore(iteration=iteration, outcomes=outcomes)


def run_improve_cycle(cfg, *, cadillac_root: str | None = None,
                       max_iterations: int = _DEFAULT_MAX_ITER,
                       quiet: bool = False) -> CycleStop:
    """Run the full improve cycle. Returns a CycleStop describing termination."""
    cadillac_root = cadillac_root or os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))

    log = ImproveLog(os.path.join(cadillac_root, "improve", "log.jsonl"))

    def emit(kind: str, **kw) -> None:
        if quiet and kind not in ("error", "stop"):
            return
        if kind == "log":
            print(kw.get("msg", ""))

    saturated_streak = 0
    zero_patch_streak = 0
    prev_score: IterationScore | None = None

    for iteration in range(1, max_iterations + 1):
        emit("log", msg=f"\n══════ improve iteration {iteration} ══════")
        t_iter_start = time.time()

        # ── 1. Audit ──
        findings = run_audit(cadillac_root, cfg, emit)
        save_findings(
            findings,
            os.path.join(cadillac_root, "improve", "state",
                         f"audit-{iteration}.json"),
        )

        # ── 2. Probe ──
        score = _probe_matrix(cfg, cadillac_root, iteration, emit)
        with open(os.path.join(cadillac_root, "improve", "state",
                               f"probe-{iteration}.json"), "w") as f:
            json.dump(score.to_dict(), f, indent=2)
        emit("log", msg=f"[improve] iter{iteration} aggregate "
                         f"= {score.aggregate:.3f}")

        # ── Termination check (saturation) ──
        if is_saturated(score):
            saturated_streak += 1
            emit("log", msg=f"[improve] saturated streak: "
                             f"{saturated_streak}/{_STOP_NEEDED}")
            if saturated_streak >= _STOP_NEEDED:
                log.append({
                    "iteration": iteration, "stop_reason": "saturated",
                    "aggregate": score.aggregate,
                })
                return CycleStop(reason="saturated", iterations=iteration)
        else:
            saturated_streak = 0

        # ── 3. Correlate ──
        candidates = correlate(findings, score, cfg, emit)

        # ── 4-5. Propose + Apply (one-at-a-time) ──
        applied_sha: str | None = None
        for cand in candidates[:3]:
            finding = next((f for f in findings
                            if f.weakness_id == cand.weakness_id), None)
            if not finding:
                continue
            proposal = propose(finding, cand, cadillac_root, cfg, emit)
            if not proposal:
                continue
            save_proposal(proposal, os.path.join(
                cadillac_root, "improve", "state",
                f"proposed-{iteration}",
            ))
            result = apply_proposal(proposal, cadillac_root, emit)
            if result.success:
                applied_sha = commit_applied(result, cadillac_root,
                                              iteration=iteration)
                emit("log", msg=f"[improve/apply] {proposal.weakness_id}: "
                                 f"committed {applied_sha or '(no sha)'}")
                break  # one-success-per-iteration cap
            else:
                emit("log", msg=f"[improve/apply] {proposal.weakness_id}: "
                                 f"REJECTED — {result.reason[:200]}")

        # ── Termination check (diminishing returns) ──
        if applied_sha is None:
            zero_patch_streak += 1
            emit("log", msg=f"[improve] zero-patch streak: "
                             f"{zero_patch_streak}/{_STOP_NEEDED}")
        else:
            zero_patch_streak = 0

        # ── 6. Record ──
        log.append({
            "iteration": iteration,
            "aggregate": score.aggregate,
            "pass_rate": score.pass_rate,
            "total_retries": score.total_retries,
            "n_findings": len(findings),
            "n_candidates": len(candidates),
            "patch_applied": applied_sha,
            "elapsed_s": round(time.time() - t_iter_start, 1),
        })

        if zero_patch_streak >= _STOP_NEEDED:
            return CycleStop(reason="diminishing_returns", iterations=iteration)

        # ── Manual stop check ──
        if _check_stop_file(cadillac_root):
            return CycleStop(reason="manual", iterations=iteration)

        prev_score = score

    return CycleStop(reason="iteration_cap", iterations=max_iterations)
