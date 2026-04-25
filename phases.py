"""Phase state machine for the Cadillac agent builder."""

from enum import Enum
from dataclasses import dataclass, field


class Phase(Enum):
    ANALYZE = "analyze"    # Read-only codebase exploration (enhance mode)
    PLAN = "plan"
    DEPS = "deps"
    SCAFFOLD = "scaffold"
    REVIEW = "review"
    BUILD = "build"
    INTEGRATE = "integrate"
    VALIDATE = "validate"
    PACKAGE = "package"


PHASE_ORDER = [Phase.PLAN, Phase.DEPS, Phase.SCAFFOLD, Phase.REVIEW, Phase.BUILD, Phase.INTEGRATE, Phase.VALIDATE, Phase.PACKAGE]

# Default round budgets per phase
DEFAULT_BUDGETS = {
    Phase.ANALYZE: 0,  # Only used in enhance mode (set to 5)
    Phase.PLAN: 4,
    Phase.DEPS: 5,
    Phase.SCAFFOLD: 20,
    Phase.REVIEW: 6,  # Adversarial critic: needs rounds to read files + trace flows + produce findings
    Phase.BUILD: 30,
    Phase.INTEGRATE: 0,  # non-modular builds skip; modular gets 20
    Phase.VALIDATE: 10,
    Phase.PACKAGE: 5,
}


@dataclass
class PhaseState:
    """Tracks current phase, round counts, and transition logic."""

    current: Phase = Phase.PLAN
    round_in_phase: int = 0
    total_rounds: int = 0
    max_rounds: dict = field(default_factory=lambda: dict(DEFAULT_BUDGETS))
    max_total_rounds: int = 1000
    validate_retries: int = 0
    max_validate_retries: int = 5
    fix_required: bool = False  # True after retreat_to_build until first edit lands
    fix_mode_rounds_no_edit: int = 0  # Counts BUILD rounds while fix_required with no successful edit
    # Rounds spent in each phase; captured on advance() so end-of-build reporting
    # / phase-history recording can see actual wall budgets used.
    phase_rounds_used: dict = field(default_factory=dict)

    @property
    def phase_index(self) -> int:
        return PHASE_ORDER.index(self.current)

    @property
    def phase_label(self) -> str:
        return f"{self.current.value.upper()} (Round {self.round_in_phase}/{self.max_rounds.get(self.current, 60)})"

    @property
    def budget_remaining(self) -> int:
        return self.max_rounds.get(self.current, 60) - self.round_in_phase

    def advance(self) -> "Phase | None":
        """Move to next phase. Returns new phase or None if at end."""
        # Capture rounds spent before we zero the counter.
        prev = self.current
        self.phase_rounds_used[prev] = (
            self.phase_rounds_used.get(prev, 0) + self.round_in_phase
        )
        idx = self.phase_index
        if idx + 1 < len(PHASE_ORDER):
            self.current = PHASE_ORDER[idx + 1]
            self.round_in_phase = 0
            return self.current
        return None

    def retreat_to_build(self) -> bool:
        """After validation failure, go back to BUILD. Returns False if retries exhausted."""
        if self.validate_retries < self.max_validate_retries:
            self.validate_retries += 1
            self.current = Phase.BUILD
            self.round_in_phase = 0
            self.fix_required = True
            self.fix_mode_rounds_no_edit = 0
            return True
        return False

    def tick_fix_mode(self, had_successful_edit: bool) -> str:
        """Advance fix-mode state after one BUILD round. Returns one of:
          - "inactive"   : fix_required was False; nothing happened
          - "cleared"    : had edit this round → fix_required cleared
          - "counting"   : no edit, but counter hasn't hit fallback threshold
          - "fallback"   : 3rd consecutive no-edit round → force-clear fix_required

        Extracted so the engine BUILD loop and unit tests call the SAME logic
        (no divergence between engine inline code and test mirror).
        """
        if not self.fix_required:
            return "inactive"
        if had_successful_edit:
            self.fix_required = False
            self.fix_mode_rounds_no_edit = 0
            return "cleared"
        self.fix_mode_rounds_no_edit += 1
        if self.fix_mode_rounds_no_edit >= 3:
            self.fix_required = False
            self.fix_mode_rounds_no_edit = 0
            return "fallback"
        return "counting"

    def retreat_to_plan(self):
        """Reset to PLAN phase for re-architecture. Total rounds keep counting."""
        self.current = Phase.PLAN
        self.round_in_phase = 0

    def tick(self) -> bool:
        """Increment round counter. Returns False if budget exhausted."""
        self.round_in_phase += 1
        self.total_rounds += 1
        return self.round_in_phase <= self.max_rounds.get(self.current, 60)

    def is_over_budget(self) -> bool:
        """True if global round limit exceeded."""
        return self.total_rounds > self.max_total_rounds

    def is_terminal(self) -> bool:
        return self.current == Phase.PACKAGE


def compute_budgets(plan: dict, task_text: str = "") -> dict:
    """Compute dynamic phase budgets based on task complexity + past builds.

    Baseline formulas below scale with plan size (files, deps, modules). On
    top of that, if `.cadillac/phase_budgets.jsonl` has history from past
    builds matching this task's tags, we bump individual phases so p90 of past
    rounds-used fits within the budget with ~20% headroom.

    `task_text` is optional: when provided, past-build tag matching becomes
    tighter (e.g. pygame builds won't inherit from React builds).

    History NEVER shrinks a budget — it only expands. A baseline budget already
    picked by plan size should survive even if past runs finished early.
    """
    n_files = len(plan.get("files", []))
    n_deps = len(plan.get("dependencies", []))
    is_modular = plan.get("modular", False)
    n_modules = len(plan.get("modules", []))
    n_constraints = len(plan.get("constraints", []) or [])

    # Task-complexity nudge: long task text or many explicit constraints means
    # PLAN/BUILD need more rounds to read everything and enforce invariants.
    plan_bump = 2 if len(task_text) > 1500 else 0
    build_bump = 5 if n_constraints > 5 else 0

    budgets = {
        Phase.PLAN: 4 + plan_bump,
        Phase.DEPS: max(3, n_deps + 2),
        Phase.SCAFFOLD: max(10, n_files + 5),
        Phase.REVIEW: 6,
        Phase.BUILD: max(20, n_files * 4) + build_bump,
        Phase.INTEGRATE: (20 + max(0, n_modules - 3) * 5) if is_modular else 0,
        Phase.VALIDATE: 10,
        Phase.PACKAGE: 5,
    }

    # Pull in cross-build memory. Import here so test mocks (and test stubs
    # that don't ship memory.py) aren't forced to load it at module import.
    # Pull in cross-build memory ONLY when we can tag this task. Unrecognized
    # task text → skip: better to trust the baseline formula than to inherit a
    # pygame budget for a rust build via an all-history fallback.
    try:
        from .memory import recall_phase_stats, infer_task_tags
        tags = list(infer_task_tags(task_text)) if task_text else []
        if tags:
            phases_to_check = [Phase.SCAFFOLD, Phase.BUILD, Phase.VALIDATE]
            if is_modular:
                phases_to_check.append(Phase.INTEGRATE)
            for phase in phases_to_check:
                stats = recall_phase_stats(phase.value, tags=tags)
                if not stats:
                    continue
                # 1.2× p90: enough headroom that a slightly slower build doesn't
                # abort, without blowing up on one outlier.
                historical_budget = int(stats["p90"] * 1.2)
                budgets[phase] = max(budgets[phase], historical_budget)
    except Exception:
        # Memory is advisory — never let it break budget computation.
        pass

    budgets["max_total_rounds"] = 1000 if is_modular else 500
    return budgets
