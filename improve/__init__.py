"""Cadillac self-improvement cycle.

Public surface:
    from cadillac.improve.cli import run_improve_cycle

The cycle reads its own source, probes a fixed test matrix of real builds,
correlates failures to weaknesses via LLM, proposes patches, applies them
atomically, and either commits the improvement or reverts. Loops until the
matrix saturates, gains stop coming, or the iteration cap fires.
"""

from .scoring import IterationScore, score_iteration, is_improvement
from .log import ImproveLog
from .matrix import MATRIX, MatrixTask, run_matrix_task

__all__ = [
    "IterationScore", "score_iteration", "is_improvement",
    "ImproveLog",
    "MATRIX", "MatrixTask", "run_matrix_task",
]
