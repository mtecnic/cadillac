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


if __name__ == "__main__":
    unittest.main()
