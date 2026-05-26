"""Shared `format_for_iterate` — render runtime failures as a BUILD instruction.

Strategy-agnostic. Engine.py uses this output as the user-message handed to
the LLM on a runtime-driven retreat-to-build. The format mirrors what we
already do for CRITIC (`cadillac/critic.py:format_for_iterate`) so the LLM
sees a consistent shape regardless of which gate caught the gap.
"""

from __future__ import annotations

from .types import ProbeFailure


def format_for_iterate(failures: list[ProbeFailure], top_n: int = 8) -> str:
    """Render the top-N failures as the iterate-pass instruction.

    Caps at `top_n` so the prompt stays digestible. Engine can call this
    repeatedly if more remain, but the typical usage is one bounce per
    build.
    """
    if not failures:
        return ""
    head = failures[:top_n]
    lines = [
        "The build's tests and static checks all pass, but real user "
        "flows fail against the running artifact. These are not unit-test "
        "failures — they're observed mismatches between what the spec "
        "promises and what the running code actually does. Fix the root "
        "cause; don't disable the probe.",
        "",
    ]
    for f in head:
        lines.append(
            f"### {f.probe.story_id} [{f.probe.priority}] {f.probe.title}"
        )
        lines.append(f"  failure: {f.failure_kind} — {f.detail}")
        if f.actual:
            actual_short = f.actual.replace("\n", "\n    ")
            if len(actual_short) > 400:
                actual_short = actual_short[:400] + "…"
            lines.append(f"  actual: {actual_short}")
        if f.suggested_area:
            lines.append(f"  start here: {f.suggested_area}")
        lines.append("")
    if len(failures) > top_n:
        lines.append(
            f"(+ {len(failures) - top_n} more probe failure(s) deferred to "
            "a later pass — focus on the above first.)"
        )
    return "\n".join(lines)
