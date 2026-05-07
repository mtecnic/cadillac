"""Progress tracking — writes progress.md for human inspection, injects compact status for LLM."""

import os
import time
from dataclasses import dataclass, field

from .phases import Phase


@dataclass
class Progress:
    task: str = ""
    workspace: str = ""
    phase: str = "PLAN"
    phase_num: int = 1
    total_phases: int = 7
    round_in_phase: int = 0
    phase_budget: int = 0
    total_rounds: int = 0
    start_time: float = field(default_factory=time.time)
    planned_files: list[tuple[str, str, bool]] = field(default_factory=list)  # (path, purpose, done)
    dependencies: list[tuple[str, bool]] = field(default_factory=list)        # (name, installed)
    build_log: list[str] = field(default_factory=list)
    validation: dict[str, bool | None] = field(default_factory=lambda: {
        "syntax": None, "lint": None, "security": None, "framework": None,
        "functional": None, "run": None, "tests": None
    })
    lessons_applied: list[str] = field(default_factory=list)

    def set_phase(self, state):
        """Update from a PhaseState object."""
        self.phase = state.current.value.upper()
        self.phase_num = state.phase_index + 1
        self.round_in_phase = state.round_in_phase
        self.phase_budget = state.max_rounds.get(state.current, 60)
        self.total_rounds = state.total_rounds

    def set_plan(self, plan: dict):
        self.planned_files = [
            (f["path"], f.get("purpose", ""), False)
            for f in plan.get("files", [])
        ]
        self.dependencies = [(d, False) for d in plan.get("dependencies", [])]

    def mark_file_done(self, path: str):
        self.planned_files = [
            (p, purpose, True if p == path else done)
            for p, purpose, done in self.planned_files
        ]

    def mark_dep_done(self, name: str):
        self.dependencies = [
            (n, True if n == name else done)
            for n, done in self.dependencies
        ]

    def log(self, msg: str):
        self.build_log.append(f"R{self.total_rounds}: {msg}")
        # Keep log bounded
        if len(self.build_log) > 50:
            self.build_log = self.build_log[-30:]

    def _elapsed(self) -> str:
        s = int(time.time() - self.start_time)
        if s < 60:
            return f"{s}s"
        return f"{s // 60}m {s % 60}s"

    def to_markdown(self) -> str:
        lines = ["# Build Progress", ""]
        lines += [f"## Task", self.task, ""]
        lines += [f"## Status: {self.phase} (Round {self.round_in_phase}/{self.phase_budget}) | Phase {self.phase_num}/{self.total_phases} | Elapsed: {self._elapsed()}", ""]

        if self.planned_files:
            lines.append("## Plan")
            for path, purpose, done in self.planned_files:
                check = "x" if done else " "
                desc = f" - {purpose}" if purpose else ""
                lines.append(f"- [{check}] {path}{desc}")
            lines.append("")

        if self.dependencies:
            lines.append("## Dependencies")
            for name, done in self.dependencies:
                check = "x" if done else " "
                lines.append(f"- [{check}] {name}")
            lines.append("")

        if self.build_log:
            lines.append("## Build Log")
            for entry in self.build_log[-15:]:
                lines.append(f"- {entry}")
            lines.append("")

        lines.append("## Validation")
        for check, result in self.validation.items():
            if result is None:
                lines.append(f"- [ ] {check.title()} (not started)")
            elif result:
                lines.append(f"- [x] {check.title()}")
            else:
                lines.append(f"- [FAIL] {check.title()}")
        lines.append("")

        if self.lessons_applied:
            lines.append("## Lessons Applied")
            for lesson in self.lessons_applied:
                lines.append(f"- {lesson}")
            lines.append("")

        return "\n".join(lines)

    def to_context(self) -> str:
        """Compact version for system prompt injection."""
        parts = [f"[{self.phase} R{self.round_in_phase}/{self.phase_budget} | Phase {self.phase_num}/{self.total_phases} | {self._elapsed()}]"]

        done_files = sum(1 for _, _, d in self.planned_files if d)
        total_files = len(self.planned_files)
        if total_files:
            parts.append(f"Files: {done_files}/{total_files} written")

        remaining = [p for p, _, d in self.planned_files if not d]
        if remaining and len(remaining) <= 5:
            parts.append(f"Remaining: {', '.join(remaining)}")

        val_items = []
        for check, result in self.validation.items():
            if result is True:
                val_items.append(f"{check}:OK")
            elif result is False:
                val_items.append(f"{check}:FAIL")
        if val_items:
            parts.append(f"Validation: {', '.join(val_items)}")

        return " | ".join(parts)

    def write(self):
        """Write progress.md to workspace, atomically.

        Atomic rename — a crash mid-write would have left progress.md
        truncated. The file is read by humans during long builds, so a
        torn write is a real UX issue. (Audit M1.)
        """
        if not self.workspace:
            return
        from ._atomic import atomic_write_text
        path = os.path.join(self.workspace, "progress.md")
        atomic_write_text(path, self.to_markdown())
