"""Round 2 of the audit: phase-state integrity (H4, H7, M4).

These cover state-machine bugs that cause phase_rounds_used corruption
and silent failure-passes when retries exhaust.
"""

import unittest

from cadillac import engine as _engine_mod
from cadillac.phases import Phase, PhaseState

_ENGINE_PATH = _engine_mod.__file__


class TestPhaseStateAdvanceRecordsRounds(unittest.TestCase):
    """H4: state.advance() must record rounds_used for the OUTGOING phase.

    The buggy path used `state.current = Phase.VALIDATE` directly,
    skipping advance(), which meant phase_rounds_used[Phase.WIRING] never
    got captured. The phase-budget memory subsystem then learned
    incorrect "WIRING took 0 rounds" data, biasing future builds.
    """

    def test_advance_captures_rounds_in_outgoing_phase(self):
        s = PhaseState()
        s.current = Phase.WIRING
        s.round_in_phase = 3
        s.advance()
        self.assertEqual(s.current, Phase.VALIDATE,
                         "WIRING should advance to VALIDATE")
        self.assertEqual(s.phase_rounds_used.get(Phase.WIRING), 3,
                         "advance() must record rounds_used for the outgoing phase")

    def test_advance_accumulates_across_revisits(self):
        """If the loop bounces WIRING → BUILD → ... → WIRING → VALIDATE,
        the WIRING rounds should accumulate, not overwrite."""
        s = PhaseState()
        s.current = Phase.WIRING
        s.round_in_phase = 2
        s.advance()  # → VALIDATE, records WIRING=2
        # Synthetic re-visit: pretend we got bounced back
        s.current = Phase.WIRING
        s.round_in_phase = 5
        s.advance()
        self.assertEqual(s.phase_rounds_used.get(Phase.WIRING), 7,
                         "rounds across visits should sum, not replace")


class TestRetreatToBuildExhaustion(unittest.TestCase):
    """H7: retreat_to_build returns False when max retries are reached.
    The engine must check that return value, not assume retreat succeeded."""

    def test_retreat_returns_true_when_under_max(self):
        s = PhaseState(validate_retries=2, max_validate_retries=5)
        s.current = Phase.VALIDATE
        s.round_in_phase = 5
        self.assertTrue(s.retreat_to_build())
        self.assertEqual(s.current, Phase.BUILD)

    def test_retreat_returns_false_at_max(self):
        s = PhaseState(validate_retries=5, max_validate_retries=5)
        s.current = Phase.VALIDATE
        self.assertFalse(s.retreat_to_build(),
                         "retreat at max retries must return False")
        # State must NOT change to BUILD when retreat fails
        self.assertEqual(s.current, Phase.VALIDATE,
                         "current phase should be unchanged when retreat fails")


class TestAdversarialBlockMessageContent(unittest.TestCase):
    """M4 + H7 outcome: when WIRING retries exhaust or adversarial retreat
    is blocked, the engine should emit explicit error context to the log.

    We assert this at the source-code level: the relevant emit() calls
    must mention 'residual' / 'remain' / 'exhausted' so a human reading
    the log knows the build proceeded with known errors rather than
    silently glossing over them.
    """

    def test_engine_has_explicit_residual_messages(self):
        """The audit fix added 'residual' tags to the WIRING-exhausted
        and adversarial-blocked paths. Pin them so a future regression
        that strips them will fail this test."""
        with open(_ENGINE_PATH) as f:
            src = f.read()
        # WIRING residual error logging
        self.assertIn("wiring/error-residual", src,
                      "WIRING-exhausted path must emit residual-error log lines")
        # Adversarial retreat-blocked logging
        self.assertIn("adv-residual", src,
                      "adversarial retreat-blocked path must emit residual log lines")
        # Phrases that prove the user-visible message is informative
        self.assertIn("retries exhausted", src.lower())
        self.assertIn("retreat to build blocked", src.lower())

    def test_engine_uses_advance_not_direct_mutation_in_wiring_exhausted(self):
        """H4 lock-in: the post-fix code MUST call state.advance() and not
        set state.current directly. Find the WIRING-exhausted path and
        check its body."""
        with open(_ENGINE_PATH) as f:
            src = f.read()
        # The exhausted block has the marker comment "(H4, M4)" added in the fix.
        # Just before the body's continue/break, we expect state.advance()
        # rather than state.current = Phase.VALIDATE.
        # Locate the block by its diagnostic emit
        idx = src.find("retries exhausted")
        self.assertGreater(idx, 0, "could not locate the WIRING-exhausted block")
        # Look at the next 600 chars for either advance() or direct mutation
        block = src[idx:idx + 1200]
        self.assertIn("state.advance()", block,
                      "exhausted block must call state.advance() — direct "
                      "mutation corrupts phase_rounds_used (H4)")
        self.assertNotIn("state.current = Phase.VALIDATE", block,
                         "direct state.current mutation regressed — was the "
                         "audit-fix reverted?")


if __name__ == "__main__":
    unittest.main()
