"""Tests for the surface parity contract and the fail-closed posture.

Two separate concerns, both about silent degradation:

PARITY — four consumers each re-implement their own `kind ==` dispatch over
one emitter. A new event kind renders wherever its author was looking and
vanishes elsewhere. `--plain` is what runs in automation, so a gap there is
invisible until someone needs the missing signal to debug a failed build.
These tests pin the declaration against the real code in BOTH directions, so
neither the code nor the declaration can drift.

FAIL-CLOSED — Cadillac runs unattended. Where a gate cannot evaluate its
input, it must deny/degrade safely rather than pass through. Pinned here so a
future refactor cannot quietly turn a defensive `except` into an open door.
"""

import os
import unittest

from cadillac import surfaces
from cadillac.surfaces import (
    EVENT_KINDS,
    GENERIC_SURFACES,
    PER_KIND_SURFACES,
    kinds_emitted_by,
    kinds_handled_by,
    parity_report,
)

_PKG = os.path.dirname(os.path.dirname(os.path.abspath(surfaces.__file__)))
_ROOT = os.path.join(_PKG, "cadillac")


def _read(name):
    with open(os.path.join(_ROOT, name)) as f:
        return f.read()


class TestRegistryIsComplete(unittest.TestCase):
    """Every kind the engine emits must be declared."""

    def test_every_emitted_kind_is_declared(self):
        emitted = kinds_emitted_by(_read("engine.py"))
        undeclared = sorted(emitted - set(EVENT_KINDS))
        self.assertEqual(
            undeclared, [],
            "engine.py emits event kinds absent from surfaces.EVENT_KINDS: "
            f"{undeclared}. Declare each one and state which surfaces render it.",
        )

    def test_no_declared_kind_is_dead(self):
        """A declared kind nothing emits is stale documentation."""
        emitted = set()
        for module in ("engine.py", "validate.py", "modules.py"):
            emitted |= kinds_emitted_by(_read(module))
        dead = sorted(set(EVENT_KINDS) - emitted)
        self.assertEqual(dead, [], f"declared but never emitted: {dead}")

    def test_surfaces_named_are_real(self):
        for name, entry in EVENT_KINDS.items():
            for surface in entry.surfaces:
                self.assertIn(surface, PER_KIND_SURFACES, f"{name} names unknown surface")
            for surface in entry.gaps:
                self.assertIn(surface, PER_KIND_SURFACES, f"{name} gap names unknown surface")


class TestDeclarationMatchesCode(unittest.TestCase):
    """The declaration must not rot into documentation that lies."""

    def setUp(self):
        self.actual = {
            "plain": kinds_handled_by(_read("events.py")),
            "rich": kinds_handled_by(_read("display.py")),
        }

    def test_declared_coverage_is_real(self):
        for name, entry in EVENT_KINDS.items():
            for surface in entry.surfaces:
                self.assertIn(
                    name, self.actual[surface],
                    f"surfaces.py claims '{surface}' renders '{name}', but its "
                    f"handler has no `kind == \"{name}\"` branch.",
                )

    def test_declared_gaps_are_real(self):
        for name, entry in EVENT_KINDS.items():
            for surface in entry.gaps:
                self.assertNotIn(
                    name, self.actual[surface],
                    f"surfaces.py declares a '{surface}' gap for '{name}', but "
                    f"the handler does render it — remove the stale gap entry.",
                )

    def test_handlers_render_nothing_undeclared(self):
        for surface, handled in self.actual.items():
            undeclared = sorted(handled - set(EVENT_KINDS))
            self.assertEqual(
                undeclared, [],
                f"{surface} handles undeclared kinds: {undeclared}",
            )


class TestNoSilentOmissions(unittest.TestCase):
    """A surface may skip a kind — but only with a stated reason."""

    def test_every_gap_has_a_reason(self):
        for name, entry in EVENT_KINDS.items():
            for surface in PER_KIND_SURFACES:
                if surface in entry.surfaces:
                    continue
                self.assertIn(
                    surface, entry.gaps,
                    f"'{name}' is not rendered by '{surface}' and gives no reason. "
                    f"Either render it or declare the gap.",
                )
                self.assertTrue(entry.gaps[surface].strip(), f"empty gap reason for {name}")

    def test_plain_surface_covers_operational_kinds(self):
        """--plain runs in automation; the kinds needed to diagnose a failed
        build must never be rich-only."""
        for name in ("error", "retry", "validation", "complete", "phase",
                     "module_complete", "file_written"):
            self.assertIn("plain", EVENT_KINDS[name].surfaces, name)

    def test_retry_is_rendered_everywhere(self):
        """The gap this contract caught on its first run."""
        self.assertEqual(set(EVENT_KINDS["retry"].surfaces), set(PER_KIND_SURFACES))

    def test_generic_surfaces_are_kind_agnostic(self):
        """jsonl/dash must stay generic — if either grows a per-kind branch it
        can develop gaps and belongs in PER_KIND_SURFACES."""
        self.assertEqual(set(GENERIC_SURFACES), {"jsonl", "dash"})
        self.assertNotIn("kind ==", _read("engine.py")[
            _read("engine.py").find("class BuildLogger"):
            _read("engine.py").find("class BuildLogger") + 1200
        ])


class TestParityReport(unittest.TestCase):
    def test_report_renders(self):
        report = "\n".join(parity_report())
        self.assertIn("retry", report)
        self.assertIn("Declared gaps", report)

    def test_report_lists_every_kind(self):
        report = "\n".join(parity_report())
        for name in EVENT_KINDS:
            self.assertIn(name, report)


class TestFailClosedPosture(unittest.TestCase):
    """Where a gate cannot evaluate its input, it must not pass through."""

    def test_command_policy_denies_on_internal_error(self):
        import cadillac.tools as tools

        original = tools._check_command_shape
        tools._check_command_shape = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            allowed, _ = tools.validate_command("npm test", workspace="/tmp")
            self.assertFalse(allowed, "policy analyzer error must deny, not allow")
        finally:
            tools._check_command_shape = original

    def test_corrupt_checkpoint_does_not_raise(self):
        import tempfile

        from cadillac.engine import _load_checkpoint

        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, ".cadillac"))
            with open(os.path.join(td, ".cadillac", "checkpoint.json"), "w") as f:
                f.write("{corrupt")
            self.assertIsNone(_load_checkpoint(td))

    def test_unknown_tool_is_refused_not_dispatched(self):
        import tempfile

        from cadillac.manifest import FileManifest
        from cadillac.tools import ToolExecutor

        with tempfile.TemporaryDirectory() as td:
            ex = ToolExecutor(td, FileManifest())
            self.assertIn("error", ex.dispatch("__class__", {}))
            self.assertIn("error", ex.dispatch("_full_path", {"path": "x"}))

    def test_provider_exhaustion_raises_rather_than_returning_empty(self):
        """The sentinel that used to drain the round budget silently."""
        from cadillac.engine import ProviderFailure
        self.assertTrue(issubclass(ProviderFailure, RuntimeError))

    def test_redaction_is_applied_before_persistence_not_after(self):
        """Redacting on read would leave the secret on disk."""
        source = _read("engine.py")
        logger_start = source.find("class BuildLogger")
        body = source[logger_start:logger_start + 1500]
        self.assertIn("redact_obj", body)
        self.assertLess(
            body.find("redact_obj"), body.find("self._f.write"),
            "redaction must happen before the write, not after",
        )


if __name__ == "__main__":
    unittest.main()
