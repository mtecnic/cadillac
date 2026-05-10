"""Score function for the improve loop.

Goals:
- Higher pass rate is always better.
- Fewer retries with same pass rate is better (a build that converged in
  one pass is healthier than one that needed three retreats to BUILD).
- Faster wall time is mildly better, but not at the expense of correctness.

The aggregate score is bounded so cost terms don't dominate real correctness
gains. Tuned on the existing 8-task matrix; thresholds may need revisiting
when the matrix grows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class TaskOutcome:
    """One matrix task's per-iteration result."""
    task_id: str
    passed_checks: int
    total_checks: int
    retry_rounds: int  # sum of validate_retries + adversarial_retries
    elapsed_s: float
    crashed: bool = False  # the build itself never completed
    error_summary: str = ""

    def score(self) -> float:
        """Single-task score in roughly [-inf, 1.0].

        - pass_rate dominates (max 1.0)
        - retry penalty: each retry costs 0.01
        - time penalty: each second costs 0.001 (so a 30-min build = -1.8)
        - crash: floor at -1.0 so a crashed build hurts but doesn't dwarf
          a half-passing build that took a long time
        """
        if self.crashed:
            return -1.0
        if self.total_checks == 0:
            return 0.0
        pass_rate = self.passed_checks / self.total_checks
        return pass_rate - 0.01 * self.retry_rounds - 0.001 * self.elapsed_s


@dataclass
class IterationScore:
    """Aggregate score across all matrix tasks for one iteration."""
    iteration: int
    outcomes: list[TaskOutcome] = field(default_factory=list)

    @property
    def aggregate(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(o.score() for o in self.outcomes) / len(self.outcomes)

    @property
    def total_retries(self) -> int:
        return sum(o.retry_rounds for o in self.outcomes)

    @property
    def total_elapsed_s(self) -> float:
        return sum(o.elapsed_s for o in self.outcomes)

    @property
    def pass_rate(self) -> float:
        """Fraction of (passed_checks/total_checks) summed across tasks."""
        passed = sum(o.passed_checks for o in self.outcomes)
        total = sum(o.total_checks for o in self.outcomes)
        return passed / total if total else 0.0

    @property
    def n_crashed(self) -> int:
        return sum(1 for o in self.outcomes if o.crashed)

    def to_dict(self) -> dict:
        return {
            "iteration": self.iteration,
            "aggregate": round(self.aggregate, 4),
            "pass_rate": round(self.pass_rate, 4),
            "total_retries": self.total_retries,
            "total_elapsed_s": round(self.total_elapsed_s, 1),
            "n_crashed": self.n_crashed,
            "outcomes": [
                {
                    "task_id": o.task_id,
                    "passed": o.passed_checks,
                    "total": o.total_checks,
                    "retries": o.retry_rounds,
                    "elapsed_s": round(o.elapsed_s, 1),
                    "crashed": o.crashed,
                    "error": o.error_summary[:200] if o.error_summary else "",
                }
                for o in self.outcomes
            ],
        }


def score_iteration(iteration: int, outcomes: Iterable[TaskOutcome]) -> IterationScore:
    """Convenience constructor."""
    return IterationScore(iteration=iteration, outcomes=list(outcomes))


def is_improvement(prev: IterationScore | None, curr: IterationScore,
                   *, min_aggregate_delta: float = 0.01,
                   min_retry_drop: int = 5) -> tuple[bool, str]:
    """Decide whether `curr` is an improvement over `prev`.

    Returns (is_better, reason). Reason is a short human string for the log.

    Two ways to qualify:
      1. Aggregate score went up by at least `min_aggregate_delta`
      2. Total retries dropped by at least `min_retry_drop` AND pass rate
         held steady or improved (catches the case where we made the
         pipeline more efficient without changing what it can build)
    """
    if prev is None:
        return True, "first iteration baseline"

    delta = curr.aggregate - prev.aggregate
    if delta >= min_aggregate_delta:
        return True, f"aggregate +{delta:.3f}"

    retry_drop = prev.total_retries - curr.total_retries
    pass_rate_delta = curr.pass_rate - prev.pass_rate
    if retry_drop >= min_retry_drop and pass_rate_delta >= 0:
        return True, (f"retries -{retry_drop} with pass-rate "
                      f"{'+' if pass_rate_delta > 0 else ''}{pass_rate_delta:.3f}")

    return False, (f"aggregate Δ={delta:+.3f} "
                   f"(<{min_aggregate_delta}); retries Δ={-retry_drop:+d} "
                   f"(need ≤-{min_retry_drop})")


def is_saturated(score: IterationScore, *, threshold: float = 0.95) -> bool:
    """All matrix tasks effectively passing → done."""
    return score.aggregate >= threshold and score.n_crashed == 0
