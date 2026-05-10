"""Append-only JSONL log of every improve iteration's outcome.

One line per iteration. Each line records: iteration number, score,
candidates considered, patch applied (or null with reason), timing.
The log is the source of truth for the loop's history; restart logic
reads it to know where to pick up.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .._atomic import atomic_append_lines, atomic_write_text


class ImproveLog:
    """Append-only iteration log."""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        """Add one iteration record. Lock-protected, fsynced."""
        record = {**record, "ts": record.get("ts") or time.time()}
        atomic_append_lines(self.path, [json.dumps(record, sort_keys=True)])

    def read(self) -> list[dict]:
        """Return every iteration's record, oldest first. Skips malformed lines."""
        if not os.path.isfile(self.path):
            return []
        out: list[dict] = []
        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return out

    def last_n(self, n: int) -> list[dict]:
        """Convenience: most-recent n records, oldest first within the slice."""
        return self.read()[-n:] if n > 0 else []

    def write_summary(self, summary_path: str) -> None:
        """Render a markdown summary of the run for human review."""
        records = self.read()
        if not records:
            text = "# Improve cycle log\n\nNo iterations recorded.\n"
        else:
            lines = ["# Improve cycle log", ""]
            for r in records:
                lines.append(f"## Iter {r.get('iteration')}")
                if "aggregate" in r:
                    lines.append(f"- aggregate: {r['aggregate']}")
                if "patch_applied" in r:
                    p = r["patch_applied"]
                    if p:
                        lines.append(f"- applied: `{p}` — {r.get('reason', '')}")
                    else:
                        lines.append(f"- no patch: {r.get('reason', '')}")
                if "elapsed_s" in r:
                    lines.append(f"- elapsed: {r['elapsed_s']:.1f}s")
                lines.append("")
            text = "\n".join(lines)
        atomic_write_text(summary_path, text)
