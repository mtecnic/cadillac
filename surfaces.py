"""Canonical event-kind registry and per-surface coverage contract.

Cadillac emits build events through one emitter (`events.py`) into four
consumers that each re-implement their own `kind ==` dispatch:

  * `plain`  — `events.default_print_handler` (the `--plain` / non-TTY path)
  * `rich`   — `display.LiveDisplay` (the interactive terminal UI)
  * `jsonl`  — `engine.BuildLogger` (writes `.cadillac/build.jsonl`)
  * `dash`   — `dash.py` (reads build.jsonl back, renders any kind generically)

Nothing tied those together. A new event kind added to the engine would render
in whichever surface its author happened to be looking at and silently vanish
from the others — and since `--plain` is what runs in automation and non-TTY
sessions, a gap there is invisible until someone needs the missing signal to
diagnose a failed build.

This module is the single declaration of what exists and who renders it, with
an explicit reason attached to every deliberate gap. `tests/test_surfaces.py`
pins it against the real code in both directions:

  * an event kind emitted by the engine but absent from `EVENT_KINDS` fails
  * a kind whose declared coverage disagrees with the actual handler fails

so a surface cannot silently drop a concept, and the declaration cannot rot
into documentation that lies. This caught its first real gap immediately: the
`retry` kind (added with the provider-classification work) was rendered by no
surface at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Surfaces that dispatch per event kind. `jsonl` and `dash` are deliberately
# excluded from per-kind coverage: BuildLogger serializes every event
# generically, and dash renders whatever kinds it finds in the log, so neither
# can have a per-kind gap.
PER_KIND_SURFACES = ("plain", "rich")
GENERIC_SURFACES = ("jsonl", "dash")
SURFACES = PER_KIND_SURFACES + GENERIC_SURFACES


@dataclass(frozen=True)
class EventKind:
    """One event kind and which per-kind surfaces render it."""
    name: str
    description: str
    surfaces: tuple[str, ...]
    #: Why a per-kind surface deliberately does not render this. Keyed by
    #: surface name. A kind missing from `surfaces` with no entry here is a
    #: silent omission and fails the contract test.
    gaps: dict = field(default_factory=dict)


def _k(name, description, surfaces, **gaps):
    return EventKind(name=name, description=description, surfaces=tuple(surfaces), gaps=gaps)


BOTH = PER_KIND_SURFACES

EVENT_KINDS: dict[str, EventKind] = {e.name: e for e in [
    _k("info", "Top-level informational line", BOTH),
    _k("separator", "Visual rule between sections", BOTH),
    _k("phase", "Phase transition with round/file counts", BOTH),
    _k("log", "Indented progress line from within a phase", BOTH),
    _k("error", "Recoverable error or abort diagnostic", BOTH),
    _k("complete", "Terminal build summary", BOTH),

    _k("tool_call", "A tool is about to execute", BOTH),
    _k("tool_result", "A tool returned", BOTH),
    _k("tool_parallel", "A batch of tool calls is running concurrently", BOTH),
    _k("file_written", "A workspace file was created or replaced", BOTH),

    _k("llm", "Completed provider call: latency, tokens, finish reason", BOTH),
    _k("llm_api", "Outgoing request size estimate and max output budget", BOTH),
    _k("retry", "A transient provider failure is being retried with backoff", BOTH),

    _k("validation", "Validation check results", BOTH),
    _k("lesson", "A lesson was extracted into cross-build memory", BOTH),
    _k("module_start", "A module build began", BOTH),
    _k("module_complete", "A module build finished", BOTH),

    # Streaming kinds: the plain handler writes tokens straight to stdout as
    # they arrive. The rich display owns the whole screen via a Live region
    # and cannot interleave raw token writes into it, so it renders the
    # aggregate `llm` event instead once the turn completes.
    _k("llm_stream_ttft", "Time to first streamed token", ("plain",),
       rich="Live-region display cannot interleave raw token output; the "
            "aggregate 'llm' event carries the same timing once the turn ends."),
    _k("llm_stream_token", "One streamed content token", ("plain",),
       rich="Live-region display cannot interleave raw token output; streamed "
            "text is shown via the aggregate 'llm' event."),
    _k("llm_stream_done", "Stream finished, total elapsed", ("plain",),
       rich="Paired with llm_stream_token; the aggregate 'llm' event reports "
            "the same elapsed time."),
]}


def declared_kinds() -> set[str]:
    return set(EVENT_KINDS)


def kinds_emitted_by(source: str) -> set[str]:
    """Every event kind emitted by a Python source string.

    Matches `emit("<kind>"` / `emitter.emit("<kind>"` literals. Dynamic kinds
    would be missed, but the engine only ever emits literals — and the contract
    test would fail loudly if that changed, which is the intent.
    """
    return set(re.findall(r'\bemit\(\s*["\']([a-z_]+)["\']', source))


def kinds_handled_by(source: str) -> set[str]:
    """Every event kind a handler dispatches on, from its `kind == "..."` tests."""
    return set(re.findall(r'kind\s*==\s*["\']([a-z_]+)["\']', source))


def declared_surfaces(kind: str) -> tuple[str, ...]:
    entry = EVENT_KINDS.get(kind)
    return entry.surfaces if entry else ()


def parity_report() -> list[str]:
    """Human-readable coverage matrix with every declared gap spelled out."""
    lines = [f"{'event kind':22} " + " ".join(f"{s:>6}" for s in PER_KIND_SURFACES)]
    lines.append("-" * len(lines[0]))
    for name, entry in EVENT_KINDS.items():
        cells = " ".join(
            f"{('yes' if s in entry.surfaces else 'GAP'):>6}" for s in PER_KIND_SURFACES
        )
        lines.append(f"{name:22} {cells}")
    gaps = [
        f"  {name} / {surface}: {reason}"
        for name, entry in EVENT_KINDS.items()
        for surface, reason in entry.gaps.items()
    ]
    if gaps:
        lines.append("")
        lines.append("Declared gaps:")
        lines.extend(gaps)
    return lines
