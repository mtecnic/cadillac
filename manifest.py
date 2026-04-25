"""File tracking, error detection, and batch management."""

import os
import re
import threading
from collections import OrderedDict


class FileManifest:
    """Tracks all files the agent has written with structural summaries."""

    def __init__(self):
        self.files: dict[str, dict] = OrderedDict()  # path -> {lines, bytes, summary, version}
        self._lock = threading.Lock()

    def record(self, path: str, content: str):
        lines = content.count("\n") + 1
        summary_parts = []
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                summary_parts.append(stripped)
            elif stripped.startswith("class "):
                summary_parts.append(stripped.split(":")[0] + ":")
            elif stripped.startswith("def "):
                sig = stripped.rstrip()
                summary_parts.append("  " + (sig if sig.endswith(":") else stripped.split(":")[0] + ":"))
            elif stripped.startswith(("@app.", "@router.")):
                summary_parts.append(stripped)
            elif re.match(r'^[A-Z][A-Z_0-9]+ *=', stripped):
                summary_parts.append(stripped[:80])
        summary = "\n".join(summary_parts[:50])
        with self._lock:
            ver = self.files.get(path, {}).get("version", 0) + 1
            self.files[path] = {"lines": lines, "bytes": len(content), "summary": summary, "version": ver}

    def remove(self, path: str):
        with self._lock:
            self.files.pop(path, None)

    def get_summary(self, path: str) -> str | None:
        with self._lock:
            info = self.files.get(path)
            return info["summary"] if info else None

    def to_status(self) -> str:
        with self._lock:
            if not self.files:
                return "No files written yet."
            parts = [f"  {p} ({info['lines']} lines, v{info['version']})" for p, info in self.files.items()]
        return "Files in workspace:\n" + "\n".join(parts)

    def to_detailed(self) -> str:
        with self._lock:
            if not self.files:
                return ""
            items = list(self.files.items())
        parts = []
        for path, info in items:
            parts.append(f"── {path} ({info['lines']} lines, v{info['version']}) ──")
            if info["summary"]:
                parts.append(info["summary"])
            parts.append("")
        return "\n".join(parts)


class ErrorTracker:
    """Tracks error signatures with classified recovery strategies."""

    ERROR_PATTERNS = [
        (r'(ImportError|ModuleNotFoundError): (.+)', 'import'),
        (r'(SyntaxError): (.+)', 'syntax'),
        (r'(AttributeError): (.+)', 'attribute'),
        (r'(NameError): (.+)', 'name'),
        (r'(TypeError): (.+)', 'type'),
        (r'(KeyError): (.+)', 'key'),
        (r'(IndexError): (.+)', 'index'),
        (r'(ValueError): (.+)', 'value'),
        (r'(FileNotFoundError): (.+)', 'file'),
        (r'Timed out', 'timeout'),
        (r'database.*(?:locked|malformed|corrupt)', 'db_corrupt'),
    ]

    # Strategy-specific intervention messages per error class
    ERROR_STRATEGIES = {
        "syntax": {
            "max_retries": 2,
            "message": (
                "SYNTAX ERROR detected. Do this:\n"
                "1. Read the FULL file that has the syntax error\n"
                "2. Look for: missing colons, unmatched brackets, bad indentation, unterminated strings\n"
                "3. Fix ALL syntax issues in ONE edit_file call"
            ),
        },
        "import": {
            "max_retries": 3,
            "message": (
                "IMPORT ERROR detected. Do this:\n"
                "1. Check if the module file exists: list_files('.')\n"
                "2. Check if the file shadows a stdlib module (io.py, os.py, json.py, csv.py etc.)\n"
                "   If so, rename it to something unique and update ALL imports across ALL files\n"
                "3. If importing a function/class, use search_files to find what the module actually exports\n"
                "4. Fix ALL import references in ONE edit_file call per file"
            ),
        },
        "attribute": {
            "max_retries": 2,
            "message": (
                "ATTRIBUTE ERROR detected. Do this:\n"
                "1. Read the FULL source file of the class/module that has the missing attribute\n"
                "2. Check the architecture for the correct method/attribute names\n"
                "3. Fix the mismatch in ONE edit_file call"
            ),
        },
        "timeout": {
            "max_retries": 1,
            "message": (
                "TIMEOUT detected. The code is hanging. Do this:\n"
                "1. Do NOT retry the same command — it will time out again\n"
                "2. Read the entry point and find blocking operations (infinite loops, async servers, input())\n"
                "3. For servers: add a shutdown signal or asyncio.wait_for with timeout\n"
                "4. For --test mode: ensure it runs tests and exits, no long-running processes\n"
                "5. Simplify: remove the blocking operation or add a timeout wrapper"
            ),
        },
        "db_corrupt": {
            "max_retries": 1,
            "message": (
                "DATABASE CORRUPTION detected. Do this:\n"
                "1. Remove FTS5 content-sync tables — they cause corruption. Use LIKE queries instead\n"
                "2. Remove complex triggers — use simple INSERT/UPDATE\n"
                "3. Delete the .db file: run_command('rm -f *.db')\n"
                "4. Run the test again"
            ),
        },
        "name": {
            "max_retries": 2,
            "message": (
                "NAME ERROR detected — a variable or function is not defined. Do this:\n"
                "1. Read the file where the error occurs\n"
                "2. Check if it's a missing import, typo, or scope issue\n"
                "3. Fix in ONE edit_file call"
            ),
        },
    }

    def __init__(self, manifest: FileManifest):
        self.history: list[tuple[str, str]] = []
        self.manifest = manifest
        self._class_counts: dict[str, int] = {}  # error class -> intervention count
        # Optional — wired from engine so auto-escalation can write tried_failed entries
        # and surface recent failures back into the intervention message.
        self.scratch = None        # cadillac.scratch.Scratch instance
        self.phase_name: str = "?"
        self.round_num: int = 0
        self.last_command: str = ""  # Set by engine before record() — used for auto-scratch context

    def _extract_signature(self, stderr: str) -> str | None:
        for pattern, category in self.ERROR_PATTERNS:
            m = re.search(pattern, stderr, re.IGNORECASE)
            if m:
                return f"{category}:{m.group(0)[:100]}"
        lines = [l.strip() for l in stderr.strip().split("\n") if l.strip()]
        return f"other:{lines[-1][:100]}" if lines else None

    def _get_class(self, sig: str) -> str:
        """Extract error class from signature."""
        return sig.split(":")[0] if ":" in sig else "other"

    def is_stuck(self) -> bool:
        """Detect if agent is making no progress — same error class triggered 3+ interventions."""
        return any(count >= 3 for count in self._class_counts.values())

    def distinct_error_classes(self) -> int:
        """Count how many different error classes have been seen."""
        classes = set()
        for sig, _ in self.history:
            cls = self._get_class(sig)
            classes.add(cls)
        return len(classes)

    def summary(self) -> str:
        """Human-readable summary of all errors seen."""
        class_counts: dict[str, int] = {}
        for sig, _ in self.history:
            cls = self._get_class(sig)
            class_counts[cls] = class_counts.get(cls, 0) + 1
        if not class_counts:
            return "No errors recorded"
        parts = [f"{count} {cls.title()}Error(s)" for cls, count in sorted(class_counts.items(), key=lambda x: -x[1])]
        return ", ".join(parts)

    def reset(self):
        """Clear all error history for re-planning."""
        self.history.clear()
        self._class_counts.clear()

    def record(self, stderr: str) -> str | None:
        """Record an error. Returns classified intervention if loop detected."""
        sig = self._extract_signature(stderr)
        if not sig:
            return None
        self.history.append((sig, stderr[:500]))

        error_class = self._get_class(sig)
        recent = self.history[-6:]
        count = sum(1 for s, _ in recent if s == sig)

        strategy = self.ERROR_STRATEGIES.get(error_class, {})
        max_retries = strategy.get("max_retries", 3)

        if count >= max_retries:
            # Track how many times we've intervened for this class
            self._class_counts[error_class] = self._class_counts.get(error_class, 0) + 1

            # Get class-specific intervention
            intervention = strategy.get("message", (
                f"STOP. The same {error_class} error has occurred {count} times:\n"
                f"  {sig}\n"
                "You are in a fix loop. Do this NOW:\n"
                "1. Read the FULL source file causing the error\n"
                "2. Identify ALL issues (not just the one error)\n"
                "3. Fix EVERYTHING in ONE edit_file call\n"
                "Do NOT make incremental single-line fixes."
            ))

            # For import errors, append module exports
            if error_class == "import":
                module_match = re.search(r"from (\w+)", stderr) or re.search(r"import (\w+)", stderr)
                if module_match:
                    mod_name = module_match.group(1)
                    for path in self.manifest.files:
                        if os.path.splitext(os.path.basename(path))[0] == mod_name:
                            summary = self.manifest.get_summary(path)
                            if summary:
                                intervention += f"\n\nHere is what {path} actually exports:\n{summary}"
                            break

            # Auto-log to scratch so the LLM's failure history accumulates even when it
            # ignores the note_lesson tool. Then re-surface recent tried_failed notes
            # in the intervention itself so the model sees its own history on next turn.
            if self.scratch is not None:
                sig_body = sig.split(":", 1)[1] if ":" in sig else sig
                cmd_tail = (self.last_command or "").strip().split("\n")[0][:40]
                note = f"{error_class}: {sig_body[:120]} (x{count})"
                if cmd_tail:
                    note += f" — cmd: {cmd_tail}"
                try:
                    self.scratch.append(
                        category="tried_failed", content=note,
                        phase=self.phase_name, round_num=self.round_num,
                    )
                except Exception:
                    pass
                try:
                    recent = _recent_scratch_failures(self.scratch, limit=3)
                except Exception:
                    recent = ""
                if recent:
                    intervention += (
                        "\n\nYOUR RECENT tried_failed NOTES (most recent 3):\n"
                        f"{recent}\n"
                        "Do NOT repeat these approaches. Try a different angle."
                    )

            self.history = self.history[-2:]  # Keep some history for convergence detection
            return intervention
        return None


def _recent_scratch_failures(scratch, limit: int = 3) -> str:
    """Return the last `limit` tried_failed entries from a Scratch as plain text."""
    raw = scratch._read_raw()
    if not raw:
        return ""
    sections = scratch._parse_sections(raw)
    entries = sections.get("tried_failed", [])[-limit:]
    return "\n".join(entries)


class BatchTracker:
    """Tracks scaffolding plan progress and generates batch directives."""

    def __init__(self, plan: dict, run_cmd: str | None = None, entry_point: str | None = None):
        self.plan = plan
        self.run_cmd = run_cmd or plan.get("run_cmd") or "python3"
        self.entry_point = entry_point or plan.get("entry_point") or "main.py"
        self.build_order: list[dict] = []
        for batch in plan.get("build_order", []):
            for f in batch.get("files", []):
                self.build_order.append({"batch": len(self.build_order) + 1, "files": [f]})
        self.current_batch_idx = 0
        self.written_files: set[str] = set()
        self.all_planned_files = {f for batch in self.build_order for f in batch.get("files", [])}

    def file_written(self, path: str) -> str | None:
        """Record that a file was written. Returns a nudge if batch advanced."""
        self.written_files.add(path)
        if self.current_batch_idx >= len(self.build_order):
            return None

        batch_files = set(self.build_order[self.current_batch_idx].get("files", []))
        if not (batch_files - self.written_files):
            self.current_batch_idx += 1
            if self.current_batch_idx >= len(self.build_order):
                return (
                    "[ALL BATCHES COMPLETE] All planned files written. "
                    f"Run `{self.run_cmd} {self.entry_point} --test` NOW to test. "
                    "Only fix errors that actually appear."
                )
            next_file = self.build_order[self.current_batch_idx]["files"][0]
            plan_files = {f["path"]: f for f in self.plan.get("files", [])}
            info = plan_files.get(next_file, {})
            purpose = info.get("purpose", "")
            detail = f"{next_file}: {purpose}" if purpose else next_file
            return (
                f"[FILE {self.current_batch_idx + 1}/{len(self.build_order)}] "
                f"Now write: {detail}\n"
                f"Write ONLY this one file using write_file. Do not write multiple files."
            )
        return None

    def get_current_directive(self) -> str | None:
        if self.current_batch_idx >= len(self.build_order):
            return None
        batch = self.build_order[self.current_batch_idx]
        remaining = [f for f in batch.get("files", []) if f not in self.written_files]
        if not remaining:
            return None

        plan_files = {f["path"]: f for f in self.plan.get("files", [])}
        file_details = []
        for f in remaining:
            info = plan_files.get(f, {})
            detail = f"  - {f}"
            if info.get("purpose"):
                detail += f": {info['purpose']}"
            if info.get("depends_on"):
                detail += f" (depends on: {', '.join(info['depends_on'])})"
            if info.get("exports"):
                detail += f" (exports: {', '.join(info['exports'])})"
            if info.get("interfaces"):
                detail += f"\n    Signatures: {'; '.join(info['interfaces'])}"
            file_details.append(detail)

        return (
            f"[FILE {self.current_batch_idx + 1}/{len(self.build_order)}] "
            f"Write this file now using write_file:\n" +
            "\n".join(file_details) +
            "\n\nWrite ONLY this one file. Do not explain — just write the code."
        )

    def get_remaining_nudge(self) -> str | None:
        if self.current_batch_idx >= len(self.build_order):
            return None
        batch_files = set(self.build_order[self.current_batch_idx].get("files", []))
        remaining = batch_files - self.written_files
        if remaining:
            return (
                f"[FILE {self.current_batch_idx + 1}/{len(self.build_order)}] "
                f"Still need to write: {', '.join(sorted(remaining))}\n"
                f"Write them now using write_file."
            )
        return None

    @property
    def all_done(self) -> bool:
        return self.current_batch_idx >= len(self.build_order)
