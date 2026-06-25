"""Tests for the prefix-cache-friendly prompt layout.

Pins the structural invariants the 2026-06-25 refactor was about:

  1. Scratch lives in its OWN user message — never the system prompt.
     If we ever regress and merge it back into the system prompt, vLLM's
     prefix cache invalidates every time scratch refreshes (every 3 rounds),
     wiping out the 8× speedup we measured on .39.

  2. spec_block flows through the prompt builder via kwarg, not via raw
     string concat in engine.py. Six engine.py call sites used to do
     `prompt += "\\n\\n" + spec_block` — that put spec_block at the very
     tail of the prompt, which meant any volatile-field change ahead of
     it invalidated the cache. Now spec_block sits at a stable middle
     position INSIDE the template.

  3. _BUILD_TEMPLATE's field order is "stable up front, volatile at the
     back": lessons → spec → manifest → code_map → progress → failures.
     Same for _SCAFFOLD_TEMPLATE: lessons → spec → manifest → progress.

If a future refactor breaks any of these without intent, this test file
fails and surfaces the regression directly.
"""

from __future__ import annotations

import unittest

from cadillac.engine import (
    _SCRATCH_MARKER,
    _build_messages,
    _refresh_scratch_in_messages,
    _sanitize_messages_for_send,
)
from cadillac.languages import python_language
from cadillac.prompts import (
    build_architecture_prompt,
    build_build_prompt,
    build_manifest_prompt,
    build_modular_architecture_prompt,
    build_modular_manifest_prompt,
    build_scaffold_prompt,
)


# ─────────────────────── Change 1: scratch placement ───────────────────────


class TestScratchInUserMessage(unittest.TestCase):
    def test_scratch_in_user_message_not_system(self):
        m = _build_messages("SYSTEM_PROMPT", "TASK", scratch_text="my note")
        self.assertEqual(m[0]["role"], "system")
        self.assertEqual(m[0]["content"], "SYSTEM_PROMPT",
                          "system prompt must NOT carry scratch — that would"
                          " invalidate vLLM's prefix cache on refresh")
        self.assertEqual(m[1]["role"], "user")
        self.assertTrue(m[1]["content"].startswith(_SCRATCH_MARKER))
        self.assertIn("my note", m[1]["content"])

    def test_no_scratch_omits_message(self):
        m = _build_messages("SYS", "TASK")
        self.assertEqual(len(m), 2)
        self.assertEqual(m[0]["content"], "SYS")
        self.assertEqual(m[1]["content"], "TASK")

    def test_refresh_updates_scratch_in_place_not_system(self):
        m = _build_messages("SYS", "TASK", scratch_text="v1")
        before_system = m[0]["content"]
        _refresh_scratch_in_messages(m, "IGNORED_BASE", "v2 updated")
        self.assertEqual(m[0]["content"], before_system,
                          "system prompt must stay untouched on scratch refresh")
        # Scratch user message replaced with new content
        scratch_msgs = [mm for mm in m if mm.get("_kind") == "scratch"]
        self.assertEqual(len(scratch_msgs), 1)
        self.assertIn("v2 updated", scratch_msgs[0]["content"])
        self.assertNotIn("v1", scratch_msgs[0]["content"])

    def test_refresh_empty_drops_scratch_message(self):
        m = _build_messages("SYS", "TASK", scratch_text="will be cleared")
        self.assertEqual(len(m), 3)
        _refresh_scratch_in_messages(m, "IGNORED", "")
        self.assertEqual(m[0]["content"], "SYS")
        self.assertFalse(any(mm.get("_kind") == "scratch" for mm in m))

    def test_refresh_inserts_when_not_already_present(self):
        m = _build_messages("SYS", "TASK")  # no scratch
        _refresh_scratch_in_messages(m, "IGNORED", "first scratch note")
        self.assertEqual(m[0]["content"], "SYS")
        # New scratch message lands AFTER system, before user task
        self.assertEqual(m[1]["role"], "user")
        self.assertEqual(m[1].get("_kind"), "scratch")
        self.assertIn("first scratch note", m[1]["content"])
        self.assertEqual(m[2]["content"], "TASK")

    def test_sanitizer_strips_kind_field(self):
        m = _build_messages("SYS", "TASK", scratch_text="note")
        clean = _sanitize_messages_for_send(m)
        for mm in clean:
            self.assertNotIn("_kind", mm,
                              "_kind is private bookkeeping; OpenAI-spec endpoints"
                              " can reject unknown top-level fields")
        # Content survives
        scratch_msg = next(mm for mm in clean
                           if mm.get("role") == "user"
                           and mm["content"].startswith(_SCRATCH_MARKER))
        self.assertIn("note", scratch_msg["content"])


# ────────────────── Change 2: spec_block via builder kwarg ──────────────────


class TestSpecBlockKwarg(unittest.TestCase):
    def test_architecture_accepts_spec_block_kwarg(self):
        without = build_architecture_prompt("lessons", lang=python_language())
        with_spec = build_architecture_prompt(
            "lessons", spec_block="MY_SPEC_BLOCK", lang=python_language())
        self.assertIn("MY_SPEC_BLOCK", with_spec)
        self.assertNotIn("MY_SPEC_BLOCK", without)
        # The spec must land at the same byte position relative to template
        # tail across calls (a stable suffix is what vLLM caches).
        self.assertTrue(with_spec.endswith("MY_SPEC_BLOCK"))

    def test_manifest_accepts_spec_block_kwarg(self):
        out = build_manifest_prompt(
            "ARCH", "lessons", spec_block="SPEC123", lang=python_language())
        self.assertIn("SPEC123", out)

    def test_build_renders_spec_inline_not_appended(self):
        """Critical: spec_block must sit BEFORE the volatile fields, not at
        the very tail. Tail-appended spec inherits cache invalidation
        from every preceding volatile field change."""
        out = build_build_prompt(
            entry_point="main.py",
            manifest_summary="MANIFEST",
            progress_context="PROGRESS",
            validation_failures="FAILS",
            lessons_text="LESSONS",
            code_map="CODEMAP",
            spec_block="SPEC_INLINE",
            lang=python_language(),
        )
        self.assertIn("SPEC_INLINE", out)
        # Spec must come BEFORE manifest, code_map, progress, failures
        self.assertLess(out.find("SPEC_INLINE"), out.find("MANIFEST"))
        self.assertLess(out.find("SPEC_INLINE"), out.find("CODEMAP"))
        self.assertLess(out.find("SPEC_INLINE"), out.find("PROGRESS"))
        self.assertLess(out.find("SPEC_INLINE"), out.find("FAILS"))

    def test_scaffold_renders_spec_inline(self):
        out = build_scaffold_prompt(
            architecture="ARCH",
            manifest_summary="MANIFEST",
            progress_context="PROGRESS",
            lessons_text="LESSONS",
            spec_block="SPEC_INLINE",
            lang=python_language(),
        )
        self.assertIn("SPEC_INLINE", out)
        self.assertLess(out.find("SPEC_INLINE"), out.find("MANIFEST"))
        self.assertLess(out.find("SPEC_INLINE"), out.find("PROGRESS"))

    def test_modular_architecture_accepts_spec(self):
        out = build_modular_architecture_prompt(
            "lessons", spec_block="MODSPEC", lang=python_language())
        self.assertIn("MODSPEC", out)

    def test_modular_manifest_accepts_spec(self):
        out = build_modular_manifest_prompt(
            "ARCH", "lessons", spec_block="MODSPEC", lang=python_language())
        self.assertIn("MODSPEC", out)

    def test_empty_spec_renders_cleanly(self):
        """Empty spec_block must render as an empty string at its position,
        not as literal `None` or a stray template token. The downstream
        fields must still appear and remain in the right order."""
        out = build_build_prompt(
            entry_point="main.py",
            manifest_summary="MANIFEST",
            progress_context="PROGRESS",
            validation_failures="",
            lessons_text="LESSONS",
            code_map="CODEMAP",
            spec_block="",
            lang=python_language(),
        )
        # Format slot should NOT leak as a literal placeholder
        self.assertNotIn("{spec_block}", out)
        self.assertNotIn("===EMPTY===", out)
        # Core fields are still present
        self.assertIn("LESSONS", out)
        self.assertIn("MANIFEST", out)
        self.assertIn("CODEMAP", out)
        # And in the right order
        self.assertLess(out.find("LESSONS"), out.find("MANIFEST"))


# ────────── Change 3: BUILD / SCAFFOLD template field order ──────────


class TestPromptFieldOrder(unittest.TestCase):
    """The cache-friendliness invariant — stable content up front, volatile
    content at the tail. If a future refactor reorders fields the wrong way,
    vLLM's cache hits collapse and builds get slower."""

    def _build_with_markers(self, *, lessons="===LESSONS===",
                             spec="===SPEC===", manifest="===MANIFEST===",
                             code_map="===CODEMAP===",
                             progress="===PROGRESS===",
                             failures="===FAILURES==="):
        return build_build_prompt(
            entry_point="main.py",
            manifest_summary=manifest,
            progress_context=progress,
            validation_failures=failures,
            lessons_text=lessons,
            code_map=code_map,
            spec_block=spec,
            lang=python_language(),
        )

    def test_build_template_stable_then_volatile_order(self):
        p = self._build_with_markers()
        # Stable → mid-stable → volatile
        lessons_pos = p.find("===LESSONS===")
        spec_pos = p.find("===SPEC===")
        manifest_pos = p.find("===MANIFEST===")
        code_pos = p.find("===CODEMAP===")
        progress_pos = p.find("===PROGRESS===")
        failures_pos = p.find("===FAILURES===")
        self.assertGreater(lessons_pos, 0)
        self.assertLess(lessons_pos, spec_pos,
                         "lessons must come before spec (both stable per build)")
        self.assertLess(spec_pos, manifest_pos,
                         "spec must come before manifest_summary (spec is more stable)")
        self.assertLess(manifest_pos, code_pos,
                         "manifest must come before code_map (grouped: file-related)")
        self.assertLess(code_pos, progress_pos,
                         "code_map must come before progress (progress is more volatile)")
        self.assertLess(progress_pos, failures_pos,
                         "validation_failures must be the very last field"
                         " (changes every retry — keep it at the tail)")

    def test_scaffold_template_stable_then_volatile_order(self):
        p = build_scaffold_prompt(
            architecture="ARCH",
            manifest_summary="===MANIFEST===",
            progress_context="===PROGRESS===",
            lessons_text="===LESSONS===",
            spec_block="===SPEC===",
            lang=python_language(),
        )
        lessons_pos = p.find("===LESSONS===")
        spec_pos = p.find("===SPEC===")
        manifest_pos = p.find("===MANIFEST===")
        progress_pos = p.find("===PROGRESS===")
        self.assertLess(lessons_pos, spec_pos)
        self.assertLess(spec_pos, manifest_pos)
        self.assertLess(manifest_pos, progress_pos,
                         "progress_context changes every round — must come last")


# ─────────────── End-to-end: stable prefix length on edits ───────────────


class TestPrefixStability(unittest.TestCase):
    """The actual cache-hit property: when a downstream volatile field
    changes, the upstream prefix must remain byte-identical."""

    def test_changing_failures_does_not_alter_prefix(self):
        """Changing validation_failures should leave everything before it
        byte-identical — that's the prefix vLLM caches."""
        p1 = build_build_prompt(
            entry_point="main.py", manifest_summary="M", progress_context="P",
            validation_failures="ERROR_V1", lessons_text="LESSONS",
            code_map="CODE", spec_block="SPEC", lang=python_language(),
        )
        p2 = build_build_prompt(
            entry_point="main.py", manifest_summary="M", progress_context="P",
            validation_failures="ERROR_V2_DIFFERENT", lessons_text="LESSONS",
            code_map="CODE", spec_block="SPEC", lang=python_language(),
        )
        # Find where they first differ — should be inside the failures block
        for i, (a, b) in enumerate(zip(p1, p2)):
            if a != b:
                first_diff = i
                break
        else:
            self.fail("prompts were identical — expected difference in failures section")
        # Everything BEFORE first_diff is the cacheable prefix
        prefix_p2 = p2[:first_diff]
        self.assertIn("LESSONS", prefix_p2)
        self.assertIn("SPEC", prefix_p2)
        self.assertIn("CODE", prefix_p2)
        # The "ERROR_V2" change should NOT touch any of lessons/spec/code_map
        # because failures sits at the tail of the template.
        self.assertNotIn("ERROR_V2", prefix_p2)

    def test_changing_code_map_preserves_lessons_and_spec_prefix(self):
        """code_map is volatile, but lessons and spec are stable — when
        only code_map changes, the prefix up to and including spec_block
        must stay byte-identical."""
        p1 = build_build_prompt(
            entry_point="main.py", manifest_summary="M", progress_context="P",
            validation_failures="", lessons_text="LESSONS",
            code_map="CODE_VERSION_1", spec_block="SPEC", lang=python_language(),
        )
        p2 = build_build_prompt(
            entry_point="main.py", manifest_summary="M", progress_context="P",
            validation_failures="", lessons_text="LESSONS",
            code_map="CODE_VERSION_2", spec_block="SPEC", lang=python_language(),
        )
        for i, (a, b) in enumerate(zip(p1, p2)):
            if a != b:
                first_diff = i
                break
        else:
            self.fail("prompts were identical")
        prefix = p1[:first_diff]
        self.assertIn("LESSONS", prefix)
        self.assertIn("SPEC", prefix)
        # code_map content should NOT be in the stable prefix (it's the volatile field)
        self.assertNotIn("CODE_VERSION_1", prefix)


if __name__ == "__main__":
    unittest.main()
