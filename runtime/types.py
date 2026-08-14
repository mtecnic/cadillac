"""Shared dataclasses for the runtime-verification package.

Each strategy (http, cli, library) defines its own `Probe` subclass holding
strategy-specific shape, but they all surface failures and results through
this common interface. The engine never case-switches on strategy — it
calls the orchestrator, gets back a `VerificationResult`, and acts on the
`failures` list.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Probe:
    """One scripted interaction. Strategy runners subclass with details."""
    story_id: str            # back-link to the spec story this probe verifies
    title: str               # short human-readable label
    priority: str            # "must" | "should" | "could" — mirrors story.priority


# ---------------------------------------------------------------------------
# Failure shape — same across all strategies so engine.py is generic
# ---------------------------------------------------------------------------


FAILURE_KINDS = (
    "status_mismatch",      # http: expect 401 got 200
    "body_mismatch",        # http: response shape wrong
    "stdout_mismatch",      # cli: expected substring missing
    "stderr_mismatch",      # cli: expected error text missing
    "exit_code_mismatch",   # cli: wrong exit code
    "import_error",         # library: snippet failed to import
    "assertion",            # library: snippet's own assert failed
    "connection_error",     # http: refused / timeout / closed
    "timeout",              # cli/library: process exceeded timeout
    "boot_failure",         # http: backend never bound or crashed at startup
    "element_not_found",    # playwright: selector matched zero nodes
    "text_mismatch",        # playwright: selector matched but text differed
    "console_error",        # playwright: page emitted uncaught error / console.error
)


@dataclass(frozen=True)
class ProbeFailure:
    """One concrete failure produced by a runner.

    The engine treats every failure uniformly: format it as a one-block
    instruction, hand to BUILD, retreat. The `suggested_area` field gives
    the LLM a starting file/symbol — best-effort, may be empty.
    """
    probe: Probe
    failure_kind: str
    detail: str              # one-line explanation, e.g. "expected status 401, got 200"
    actual: str              # the actual response / stdout / exception text
    suggested_area: str = "" # "service/auth_service.py logout handler"


# ---------------------------------------------------------------------------
# Result shape — orchestrator returns one of these per build
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationResult:
    """One run of runtime verification.

    `strategy` is set to "skip" when the project has no surface this lever
    can verify (static site, browser ext, interactive game). Skipping is a
    normal outcome, never a failure.
    """
    strategy: str            # "http" | "cli" | "library" | "skip"
    probes_run: int
    failures: tuple[ProbeFailure, ...] = ()
    skipped_reason: str = "" # populated only when strategy == "skip"

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def actionable_failures(self) -> tuple[ProbeFailure, ...]:
        """Failures the engine should bounce-to-build on.

        Drops "could"-priority failures from the actionable set — they're
        nice-to-have, and bouncing on them risks burning a retry budget
        on cosmetic issues. The engine surfaces them as advisory log
        entries instead.
        """
        return tuple(f for f in self.failures if f.probe.priority != "could")
