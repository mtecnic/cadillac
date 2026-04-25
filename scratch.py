"""Within-build scratchpad — LLM writes notes that survive phase transitions and message compression.

The scratch lives in workspace/.cadillac/scratch.md (or <module>.scratch.md for module-scoped builds).
It is re-injected into the system prompt every round, outside the messages list, so the 3-tier
context compression (engine._dedup_reads, _compress_tool_results, _summarize_middle) cannot lose it.
"""

import os
import re
from dataclasses import dataclass

VALID_CATEGORIES = ("tried_failed", "working_pattern", "reminder")
_CATEGORY_HEADERS = {
    "tried_failed": "## Tried & Failed",
    "working_pattern": "## Working Patterns",
    "reminder": "## Reminders for next phase",
}
_CONTENT_CAP = 200
_DEFAULT_READ_CAP = 3000
_DEDUP_LOOKBACK = 5
_DEDUP_OVERLAP = 0.8


def _word_overlap(a: str, b: str) -> float:
    """Jaccard similarity between word sets of two strings."""
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


@dataclass
class Scratch:
    """Per-workspace or per-module scratchpad backed by a .md file."""
    workspace: str
    module: str | None = None  # e.g. "auth" or "core/storage"; None = workspace-root scratch

    def path(self) -> str:
        scratch_dir = os.path.join(self.workspace, ".cadillac")
        if self.module:
            # Slashes in module path -> use last component for filename clarity
            safe = self.module.replace("/", "__").replace("\\", "__")
            return os.path.join(scratch_dir, f"{safe}.scratch.md")
        return os.path.join(scratch_dir, "scratch.md")

    def append(self, category: str, content: str, phase: str = "?", round_num: int = 0) -> str:
        """Append a note to the scratchpad. Returns "ok" or an error reason.

        Skips if content is too similar (>80% word overlap) to one of the last 5 entries
        in the same category. Truncates content to 200 chars.
        """
        if category not in VALID_CATEGORIES:
            return f"error: category must be one of {VALID_CATEGORIES}"
        content = (content or "").strip()
        if not content:
            return "error: empty content"
        if len(content) > _CONTENT_CAP:
            content = content[:_CONTENT_CAP].rstrip() + "..."

        existing = self._read_raw()
        recent = self._recent_entries(existing, category, _DEDUP_LOOKBACK)
        for prev in recent:
            # Strip "- [PHASE Rn] " prefix so it doesn't dilute the overlap
            prev_clean = re.sub(r"^-\s*\[[^\]]*\]\s*", "", prev)
            if _word_overlap(prev_clean, content) > _DEDUP_OVERLAP:
                return "skipped: duplicate of recent entry"

        os.makedirs(os.path.dirname(self.path()), exist_ok=True)
        sections = self._parse_sections(existing)
        new_line = f"- [{phase} R{round_num}] {content}"
        sections.setdefault(category, []).append(new_line)
        self._write_sections(sections)
        return "ok"

    def read(self, max_chars: int = _DEFAULT_READ_CAP) -> str:
        """Return scratch contents, truncated to max_chars (preserves most recent entries)."""
        text = self._read_raw()
        if len(text) <= max_chars:
            return text
        # Truncate from the start; keep the tail (most recent appends).
        head = "[scratch truncated — showing most recent entries]\n"
        budget = max_chars - len(head)
        return head + text[-budget:]

    @staticmethod
    def read_dependencies(workspace: str, dep_modules: list[str], cap_per: int = 1000) -> str:
        """Concatenate scratches of named modules. Each capped at cap_per chars.

        Returns empty string if no dep modules have scratches.
        """
        chunks = []
        for mod in dep_modules:
            s = Scratch(workspace, module=mod)
            text = s.read(max_chars=cap_per)
            if text.strip():
                chunks.append(f"### From {mod}/scratch.md\n{text}")
        return "\n\n".join(chunks)

    # ── internals ──────────────────────────────────────────────────────────

    def _read_raw(self) -> str:
        p = self.path()
        if not os.path.exists(p):
            return ""
        try:
            with open(p, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def _parse_sections(self, text: str) -> dict[str, list[str]]:
        """Parse existing scratch into {category: [entry_line, ...]}."""
        sections: dict[str, list[str]] = {c: [] for c in VALID_CATEGORIES}
        header_to_cat = {h: c for c, h in _CATEGORY_HEADERS.items()}
        current = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped in header_to_cat:
                current = header_to_cat[stripped]
                continue
            if current and stripped.startswith("- "):
                sections[current].append(stripped)
        return sections

    def _recent_entries(self, text: str, category: str, n: int) -> list[str]:
        sections = self._parse_sections(text)
        return sections.get(category, [])[-n:]

    def _write_sections(self, sections: dict[str, list[str]]) -> None:
        lines = []
        for cat in VALID_CATEGORIES:  # stable order
            entries = sections.get(cat, [])
            if not entries:
                continue
            lines.append(_CATEGORY_HEADERS[cat])
            lines.extend(entries)
            lines.append("")  # blank line between sections
        with open(self.path(), "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
