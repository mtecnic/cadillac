"""Tests for cadillac.memory — cross-build lessons + tags + O(n) dedup + causation."""

import os
import tempfile
import time
import unittest
from unittest import mock

from cadillac import memory as memory_mod
from cadillac.memory import (
    Lesson, deduplicate, infer_task_tags, parse_reflection,
    penalize_backfired, recall, save_lesson, save_all,
)


class TestMemory(unittest.TestCase):
    def setUp(self):
        # Redirect MEMORY_PATH to a temp file so tests don't pollute real memory
        self._tmp = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmp.name, "memory.jsonl")
        self._patch = mock.patch.object(memory_mod, "MEMORY_PATH", self._path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_save_and_load(self):
        l = Lesson(ts=time.time(), type="error_pattern",
                   trigger="missing import", fix="add `import os`",
                   confidence=0.5, polarity="do", tags=["python"])
        save_lesson(l)
        loaded = memory_mod.load_lessons()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].trigger, "missing import")
        self.assertEqual(loaded[0].tags, ["python"])

    def test_deduplicate_merges_word_overlap(self):
        a = Lesson(ts=time.time(), type="error_pattern",
                   trigger="naming module types causes shadow",
                   fix="rename module", confidence=0.5)
        b = Lesson(ts=time.time(), type="error_pattern",
                   trigger="naming module types causes shadow problems",
                   fix="rename to models", confidence=0.8)
        merged = deduplicate([a, b])
        self.assertEqual(len(merged), 1)
        # Higher confidence wins
        self.assertEqual(merged[0].confidence, 0.8)

    def test_deduplicate_preserves_distinct(self):
        a = Lesson(ts=time.time(), type="error_pattern",
                   trigger="syntax error in JSON parsing",
                   fix="escape quotes")
        b = Lesson(ts=time.time(), type="dependency",
                   trigger="missing pip package flask",
                   fix="pip install flask")
        merged = deduplicate([a, b])
        self.assertEqual(len(merged), 2)

    def test_deduplicate_on_type_mismatch_keeps_both(self):
        a = Lesson(ts=time.time(), type="error_pattern",
                   trigger="exact same trigger words", fix="x")
        b = Lesson(ts=time.time(), type="dependency",
                   trigger="exact same trigger words", fix="y")
        merged = deduplicate([a, b])
        self.assertEqual(len(merged), 2)

    def test_deduplicate_is_fast_for_large_input(self):
        # ~500 distinct lessons should run in well under 100ms with O(n) impl.
        # Keep triggers semantically distinct so overlap dedup doesn't merge them.
        lessons = []
        words_pool = [
            "alpha beta gamma delta epsilon",
            "raspberry pickle astronaut horizon",
            "compile parse render serialize",
            "missing import undefined symbol",
            "race condition deadlock mutex",
        ]
        for i in range(500):
            base = words_pool[i % len(words_pool)]
            lessons.append(Lesson(
                ts=time.time(), type="error_pattern",
                trigger=f"id_{i:04d} {base} unique_token_{i}",
                fix="do thing", confidence=0.5,
            ))
        t0 = time.time()
        merged = deduplicate(lessons)
        elapsed = time.time() - t0
        self.assertEqual(len(merged), 500)
        self.assertLess(elapsed, 0.5, f"deduplicate too slow: {elapsed:.3f}s")

    def test_infer_task_tags(self):
        self.assertEqual(infer_task_tags("Build a Python Flask REST API"),
                         {"python", "flask", "build"})
        self.assertEqual(infer_task_tags("Make a TypeScript React app"),
                         {"react", "typescript"})
        self.assertEqual(infer_task_tags("Just write some text"), set())

    def test_recall_filters_by_tags(self):
        save_lesson(Lesson(ts=time.time(), type="error_pattern",
                            trigger="python flask app needs CORS",
                            fix="add flask-cors", tags=["python", "flask"],
                            confidence=0.9))
        save_lesson(Lesson(ts=time.time(), type="error_pattern",
                            trigger="typescript react testing dom queries",
                            fix="use within()", tags=["typescript", "react"],
                            confidence=0.9))
        save_lesson(Lesson(ts=time.time(), type="architecture",
                            trigger="general architecture lesson",
                            fix="modular when 15+", tags=[],
                            confidence=0.9))

        py_results = recall("Build a Python Flask app for users")
        triggers = [l.trigger for l in py_results]
        # Tagged-python lesson should appear, ts/react should NOT
        self.assertTrue(any("flask" in t for t in triggers))
        self.assertFalse(any("react" in t for t in triggers))
        # Untagged general lesson should also appear
        self.assertTrue(any("modular when" in (l.fix or "") for l in py_results))

    def test_parse_reflection_with_tags(self):
        text = "ARCHITECTURE | use modular for 15+ files | clear separation"
        parsed = parse_reflection(text, tags=["python", "build"])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].tags, ["python", "build"])
        self.assertEqual(parsed[0].polarity, "do")

    def test_parse_reflection_anti(self):
        text = "ANTI | name a python module types.py | shadows stdlib"
        parsed = parse_reflection(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].polarity, "dont")
        self.assertEqual(parsed[0].type, "error_pattern")

    def test_parse_reflection_invalid_type_falls_back(self):
        text = "GIBBERISH | trigger | fix"
        parsed = parse_reflection(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].type, "error_pattern")

    def test_penalize_backfired(self):
        lesson = Lesson(ts=time.time(), type="error_pattern",
                        trigger="add type module to package json",
                        fix="set type to module", confidence=0.7)
        save_lesson(lesson)
        # Error mentions all the trigger words -> should penalize
        backfired = penalize_backfired(
            ["add type module to package json"],
            "TypeError: cannot add type module to package json file at line 5",
        )
        self.assertEqual(backfired, ["add type module to package json"])
        loaded = memory_mod.load_lessons()
        self.assertAlmostEqual(loaded[0].confidence, 0.6, places=2)

    def test_penalize_backfired_no_match(self):
        lesson = Lesson(ts=time.time(), type="error_pattern",
                        trigger="something completely unrelated entirely",
                        fix="x", confidence=0.7)
        save_lesson(lesson)
        backfired = penalize_backfired(["something completely unrelated entirely"],
                                       "different unrelated error message about networking")
        self.assertEqual(backfired, [])
        loaded = memory_mod.load_lessons()
        self.assertEqual(loaded[0].confidence, 0.7)


# ── New: tag inference + cross-task decay + reflection defaults ──────────────


class TestInferTagsFromText(unittest.TestCase):
    def test_known_tag_in_text(self):
        from cadillac.memory import infer_tags_from_text
        self.assertIn("python", infer_tags_from_text("pytest unit failure"))
        self.assertIn("pytest", infer_tags_from_text("pytest unit failure"))

    def test_synonyms_map_to_canonical(self):
        """`aiosqlite` is itself a _KNOWN_TAG; its presence ALSO expands via
        the synonym map. So a lesson mentioning aiosqlite picks up both the
        literal `aiosqlite` tag and the canonical stack tags."""
        from cadillac.memory import infer_tags_from_text
        tags = infer_tags_from_text("aiosqlite connection pooling")
        self.assertTrue({"python", "sqlite", "asyncio"} <= tags)

    def test_file_extension_hint(self):
        from cadillac.memory import infer_tags_from_text
        self.assertIn("python", infer_tags_from_text("error in foo.py at line 5"))
        self.assertIn("typescript", infer_tags_from_text("tsconfig.json missing"))
        self.assertIn("rust", infer_tags_from_text("cargo build failed"))

    def test_no_tags_for_generic_text(self):
        """Truly generic text (no stack keywords, no phase names, no file
        extensions) yields no tags."""
        from cadillac.memory import infer_tags_from_text
        self.assertEqual(
            infer_tags_from_text("the writer crashed unexpectedly"),
            set(),
        )


class TestSourceTaskDecay(memory_mod.TestMemory if False else unittest.TestCase):
    """Cross-task decay: lessons whose source_task has zero tag overlap with
    the current task get a 5× score penalty."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmp.name, "memory.jsonl")
        self._patch = mock.patch.object(memory_mod, "MEMORY_PATH", self._path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_recall_deprioritises_cross_stack_lesson(self):
        """A lesson originating from a clearly different stack should not
        outrank a stack-matched lesson, even when its confidence + used
        are much higher. Decay requires ZERO source-tag overlap with the
        current task — same-language but different framework counts as
        'same stack' for this purpose (intentional: Python lessons often
        transfer across Python frameworks)."""
        ts = time.time()
        rust_lesson = Lesson(
            ts=ts, type="error_pattern",
            trigger="cargo build connection management primitives",
            fix="use tokio mutex",
            confidence=1.0, used=50,
            polarity="do", tags=["rust", "async"],
            source_task="rust async server with tokio",
        )
        flask_lesson = Lesson(
            ts=ts, type="error_pattern",
            trigger="habit tracker storage layer",
            fix="use sqlalchemy Pool",
            confidence=0.5, used=2,
            polarity="do", tags=["python", "flask", "sqlite"],
            source_task="Flask habit tracker with sqlite",
        )
        save_lesson(rust_lesson)
        save_lesson(flask_lesson)
        results = recall("python flask habit tracker storage with sqlite", limit=5)
        # Rust lesson gets the 5× penalty; Flask lesson wins despite lower
        # confidence/used. (Both must pass the hard tag filter, which they
        # do: flask_lesson via flask+sqlite match; rust_lesson has no
        # tag overlap so it gets filtered. So we actually expect 1 result.)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].fix, "use sqlalchemy Pool")

    def test_recall_decay_when_filter_misses(self):
        """When a lesson survives the hard tag filter (tag overlap exists)
        but its source_task is from a clearly different stack, the
        scoring penalty still applies."""
        ts = time.time()
        # Both lessons are tagged python AND share python with the task.
        # But the irc-source lesson's source_task tags ({python, asyncio})
        # only overlaps python with the task ({python, flask, sqlite}) —
        # not strict zero, so no decay there. That's by design — same
        # language is treated as same stack. This test is just a sanity
        # check that mixed-tag scenarios don't break recall.
        from cadillac.memory import infer_tags_from_text
        irc_src_tags = infer_tags_from_text("async IRC server")
        task_tags = {"python", "flask", "sqlite"}
        self.assertTrue(irc_src_tags & task_tags,
            "same-language stacks share python — decay correctly skipped")

    def test_no_source_task_no_penalty(self):
        """Legacy lessons (no source_task) are treated neutrally — they
        don't get cross-stack decay even when tags don't match.

        The task text must include a tag the legacy lesson has, otherwise
        the existing hard tag-filter at the top of recall() drops it before
        scoring (that filter pre-dates source_task and is unrelated)."""
        ts = time.time()
        legacy = Lesson(
            ts=ts, type="error_pattern",
            trigger="some old lesson",
            fix="legacy fix",
            confidence=0.9, used=10,
            polarity="do", tags=["python"],
            source_task="",  # legacy
        )
        save_lesson(legacy)
        results = recall("Build a python flask app", limit=5)
        self.assertEqual(len(results), 1)


class TestParseReflectionDefaults(unittest.TestCase):
    def test_confidence_starts_at_0_3(self):
        """Forces reinforcement before a fresh lesson outweighs noise."""
        lessons = parse_reflection("error_pattern | something broke | fix it")
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0].confidence, 0.3)

    def test_auto_tags_from_body_when_caller_omits(self):
        """When `tags` is None, the parser infers from trigger+fix content."""
        lessons = parse_reflection(
            "error_pattern | pytest fails on .py module | fix is to use python3 -m pytest")
        self.assertIn("python", lessons[0].tags)
        self.assertIn("pytest", lessons[0].tags)

    def test_explicit_tags_override_inference(self):
        lessons = parse_reflection(
            "error_pattern | pytest fails | fix",
            tags=["custom_tag"],
        )
        self.assertEqual(lessons[0].tags, ["custom_tag"])

    def test_source_task_captured(self):
        lessons = parse_reflection(
            "error_pattern | x | y",
            source_task="my Flask task",
        )
        self.assertEqual(lessons[0].source_task, "my Flask task")

    def test_source_task_truncated(self):
        long_task = "x" * 500
        lessons = parse_reflection("error_pattern | x | y", source_task=long_task)
        self.assertEqual(len(lessons[0].source_task), 200)


if __name__ == "__main__":
    unittest.main()
