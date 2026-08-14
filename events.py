"""Structured event system for decoupling engine output from display."""

import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Event:
    """A structured event emitted by the engine."""
    kind: str       # phase, round, tool_call, tool_result, llm, validation, log, error, complete
    data: dict[str, Any]
    ts: float = field(default_factory=time.time)


class EventEmitter:
    """Simple synchronous event emitter with callback listeners."""

    def __init__(self):
        self._listeners: list[Callable[[Event], None]] = []

    def on(self, fn: Callable[[Event], None]):
        self._listeners.append(fn)

    def emit(self, kind: str, **data: Any):
        event = Event(kind=kind, data=data)
        for fn in self._listeners:
            try:
                fn(event)
            except Exception:
                traceback.print_exc(file=sys.stderr)  # Log but don't crash engine


def default_print_handler(event: Event):
    """Reproduce the original print() behavior from events."""
    d = event.data
    kind = event.kind

    if kind == "info":
        print(d.get("msg", ""))
    elif kind == "separator":
        print(d.get("char", "─") * d.get("width", 60))
    elif kind == "phase":
        print(f"\n{'─' * 60}")
        print(f"[{d.get('label', '')}] Total R{d.get('total_rounds', 0)} | {d.get('n_files', 0)} files")
        print(f"{'─' * 60}", flush=True)
    elif kind == "log":
        print(f"  {d.get('msg', '')}", flush=True)
    elif kind == "tool_call":
        name = d.get("name", "?")
        summary = d.get("summary", "")
        print(f"\n  >> {name}({summary})")
    elif kind == "tool_result":
        text = d.get("summary", "")
        print(f"  << {text}")
    elif kind == "tool_parallel":
        print(f"\n  [parallel: {d.get('count', 0)} tool calls]")
    elif kind == "llm":
        parts = []
        if d.get("elapsed"):
            parts.append(f"{d['elapsed']:.2f}s")
        if d.get("tokens"):
            parts.append(f"{d['tokens']} tok")
        if d.get("finish"):
            parts.append(d["finish"])
        print(f"  [{' | '.join(parts)}]", flush=True)
        if d.get("content"):
            text = d["content"]
            print(f"  {text[:300]}{'...' if len(text) > 300 else ''}", flush=True)
    elif kind == "llm_api":
        print(f"  [API ~{d.get('input_est', '?')} in, {d.get('max_out', '?')} max out]", flush=True)
    elif kind == "llm_stream_ttft":
        import sys
        sys.stdout.write(f"  [TTFT: {d.get('ttft', 0):.2f}s] ")
        sys.stdout.flush()
    elif kind == "llm_stream_token":
        import sys
        sys.stdout.write(d.get("token", ""))
        sys.stdout.flush()
    elif kind == "llm_stream_done":
        print(f"\n  [{d.get('elapsed', 0):.2f}s total]")
    elif kind == "validation":
        for r in d.get("results", []):
            status = "PASS" if r["passed"] else "FAIL"
            output = r.get("output", "OK")[:100]
            print(f"    [{status}] {r['name']}: {output}")
    elif kind == "lesson":
        print(f"  [LESSON] {d.get('type', '')}: {d.get('trigger', '')[:60]} -> {d.get('fix', '')[:60]}")
    elif kind == "module_start":
        mod = d.get("module", "?")
        n = d.get("n_files", 0)
        print(f"\n  [MODULE {mod}] Building {n} files...", flush=True)
    elif kind == "module_complete":
        mod = d.get("module", "?")
        ok = d.get("success", False)
        status = "OK" if ok else "FAIL"
        print(f"  [MODULE {mod}] {status}", flush=True)
    elif kind == "complete":
        label = d.get("status", "COMPLETE")
        print(f"\n{'═' * 60}")
        print(f"{label} | {d.get('total_rounds', 0)} rounds | {d.get('n_files', 0)} files | {d.get('elapsed', 0):.1f}s")
        print(f"Files: {d.get('files', 'none')}")
        print(f"Workspace: {d.get('workspace', '')}")
        print(f"{'═' * 60}")
    elif kind == "retry":
        print(
            f"  [RETRY {d.get('attempt', '?')}/{d.get('max_attempts', '?')}] "
            f"{d.get('reason_class', 'transient')} — waiting {d.get('delay', 0)}s",
            flush=True,
        )
    elif kind == "file_written":
        print(f"  [+] {d.get('path', '')}", flush=True)
    elif kind == "error":
        print(f"  [ERROR] {d.get('msg', '')}")
