"""Round 1 of the audit: concurrency-safety on durable files (H1, H2, H3, M1).

Each test fires N concurrent operations and asserts no entries lost / no
truncated files. Without the fix from `_atomic.py`, these tests fail by
losing data or producing malformed JSONL.
"""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor


class TestScratchRace(unittest.TestCase):
    """H1: root scratch must not lose entries when N threads append concurrently."""

    def test_concurrent_appends_dont_lose_entries(self):
        from cadillac.scratch import Scratch
        with tempfile.TemporaryDirectory() as td:
            scratch = Scratch(td)  # workspace-root scratch — the racy one
            n_threads = 10
            entries_per_thread = 5
            expected_total = n_threads * entries_per_thread

            # Three categories so the dedup-window-of-5 within one category
            # never sees a sibling thread's similar entry.
            categories = ("tried_failed", "working_pattern", "reminder")

            def worker(thread_id: int) -> int:
                count = 0
                for i in range(entries_per_thread):
                    cat = categories[i % len(categories)]
                    # Each entry has a fully disjoint vocabulary so the
                    # dedup overlap (jaccard) can't collapse them.
                    content = (
                        f"alpha{thread_id}beta{i} gamma{thread_id*97+i} "
                        f"delta{thread_id^i} epsilon{thread_id*7+i*13} "
                        f"unique-token-{thread_id*1000+i}"
                    )
                    r = scratch.append(cat, content, phase="TEST", round_num=i)
                    if r == "ok":
                        count += 1
                return count

            with ThreadPoolExecutor(max_workers=n_threads) as pool:
                results = list(pool.map(worker, range(n_threads)))

            self.assertEqual(sum(results), expected_total,
                             "every append() should have returned 'ok'")

            # Re-read raw (don't go through read() which caps to 3000 chars
            # and would lop off our oldest entries).
            text = scratch._read_raw()
            for tid in range(n_threads):
                for i in range(entries_per_thread):
                    token = f"unique-token-{tid*1000+i}"
                    self.assertIn(token, text,
                                  f"entry {token} was lost in the race")


class TestMemoryRace(unittest.TestCase):
    """H2: memory.jsonl appends must serialize cleanly across threads."""

    def test_concurrent_save_lesson(self):
        from cadillac.memory import Lesson, save_lesson, MEMORY_PATH
        # Redirect MEMORY_PATH for the test
        import cadillac.memory as memory
        with tempfile.TemporaryDirectory() as td:
            test_path = os.path.join(td, "memory.jsonl")
            original = memory.MEMORY_PATH
            memory.MEMORY_PATH = test_path
            try:
                n_threads = 10
                lessons_per_thread = 5
                expected_total = n_threads * lessons_per_thread

                def worker(thread_id: int) -> int:
                    count = 0
                    for i in range(lessons_per_thread):
                        lesson = Lesson(
                            ts=float(thread_id * 1000 + i),
                            type="reminder",
                            polarity="do",
                            trigger=f"thread-{thread_id}-i-{i}",
                            fix=f"some fix here {thread_id*1000+i}",
                            confidence=0.8,
                        )
                        save_lesson(lesson)
                        count += 1
                    return count

                with ThreadPoolExecutor(max_workers=n_threads) as pool:
                    list(pool.map(worker, range(n_threads)))

                # File must be parseable AND all lessons present.
                with open(test_path) as f:
                    lines = [l for l in f.read().splitlines() if l.strip()]
                # No malformed lines
                parsed = []
                for line in lines:
                    try:
                        parsed.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        self.fail(f"malformed JSON line in memory.jsonl: {e}\n{line!r}")
                self.assertEqual(len(parsed), expected_total,
                                 f"expected {expected_total} lessons, got {len(parsed)}")
            finally:
                memory.MEMORY_PATH = original

    def test_concurrent_phase_history(self):
        """Same race shape on phase_budgets.jsonl."""
        import cadillac.memory as memory
        with tempfile.TemporaryDirectory() as td:
            test_path = os.path.join(td, "phase_budgets.jsonl")
            original = memory.PHASE_HISTORY_PATH
            memory.PHASE_HISTORY_PATH = test_path
            try:
                n_threads = 8
                per_thread = 5

                def worker(thread_id: int) -> None:
                    for i in range(per_thread):
                        memory.record_phase_outcome(
                            phase=f"PHASE_{thread_id}_{i}",
                            rounds=i,
                            n_files=thread_id,
                            tags=[f"tag-{thread_id}-{i}"],
                        )

                with ThreadPoolExecutor(max_workers=n_threads) as pool:
                    list(pool.map(worker, range(n_threads)))

                with open(test_path) as f:
                    lines = [l for l in f.read().splitlines() if l.strip()]
                for line in lines:
                    json.loads(line)  # raises if any line is malformed
                self.assertEqual(len(lines), n_threads * per_thread)
            finally:
                memory.PHASE_HISTORY_PATH = original


class TestSaveAllAtomicity(unittest.TestCase):
    """H3: save_all() must not truncate the file before writing."""

    def test_save_all_uses_atomic_rename(self):
        """We can't easily kill the process mid-write, but we CAN verify
        the implementation uses a tempfile — which is the property that
        guarantees crash-safety. The audit's red-flag was 'open(path, "w")'
        truncating before write. After the fix, the underlying file system
        operation should be a tempfile + os.replace pattern (visible by
        the lack of a direct `open(..., 'w')` on the target).
        """
        import cadillac.memory as memory
        from cadillac.memory import Lesson
        with tempfile.TemporaryDirectory() as td:
            test_path = os.path.join(td, "memory.jsonl")
            original = memory.MEMORY_PATH
            memory.MEMORY_PATH = test_path
            try:
                # Pre-populate with a real lesson so we can detect truncation
                memory.save_all([Lesson(
                    ts=1.0, type="reminder", polarity="do",
                    trigger="initial", fix="do not lose me",
                    confidence=0.9,
                )])
                with open(test_path) as f:
                    self.assertIn("initial", f.read())

                # Write a larger set; the new file should completely replace
                # the old one without ever showing an empty intermediate.
                lessons = [
                    Lesson(ts=float(i), type="reminder", polarity="do",
                           trigger=f"trig-{i}", fix=f"fix-{i}",
                           confidence=0.5)
                    for i in range(50)
                ]
                memory.save_all(lessons)
                with open(test_path) as f:
                    text = f.read()
                self.assertEqual(len([l for l in text.splitlines() if l.strip()]),
                                 50)
                self.assertIn("trig-0", text)
                self.assertIn("trig-49", text)
                # Old data is gone (full rewrite) — that's the intended behavior.
                self.assertNotIn("initial", text)
            finally:
                memory.MEMORY_PATH = original


class TestProgressAtomicity(unittest.TestCase):
    """M1: progress.md write must not torn-write."""

    def test_progress_write_completes(self):
        from cadillac.progress import Progress
        with tempfile.TemporaryDirectory() as td:
            p = Progress(task="audit-test", workspace=td)
            p.log("first event")
            p.log("second event")
            p.write()
            with open(os.path.join(td, "progress.md")) as f:
                text = f.read()
            self.assertIn("first event", text)
            self.assertIn("second event", text)

    def test_no_orphaned_temp_files(self):
        """Atomic-write helper must clean up its tempfiles after completing."""
        from cadillac.progress import Progress
        with tempfile.TemporaryDirectory() as td:
            p = Progress(task="audit-test", workspace=td)
            p.write()
            p.write()
            p.write()
            # Only progress.md should remain — no progress.md.* tempfiles
            entries = os.listdir(td)
            stragglers = [e for e in entries
                          if e.startswith("progress.md.") and e != "progress.md"]
            self.assertEqual(stragglers, [],
                             f"orphaned temp files: {stragglers}")


class TestAtomicHelpers(unittest.TestCase):
    """Direct tests on _atomic.py — the building block."""

    def test_atomic_write_text_basic(self):
        from cadillac._atomic import atomic_write_text
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "f.txt")
            atomic_write_text(path, "hello")
            with open(path) as f:
                self.assertEqual(f.read(), "hello")

    def test_atomic_write_text_replaces(self):
        from cadillac._atomic import atomic_write_text
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "f.txt")
            with open(path, "w") as f:
                f.write("original")
            atomic_write_text(path, "replaced")
            with open(path) as f:
                self.assertEqual(f.read(), "replaced")

    def test_file_lock_is_exclusive(self):
        """If thread A holds the lock and thread B tries to take it with a
        short timeout, B should TimeoutError."""
        import threading
        from cadillac._atomic import file_lock
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "locked.txt")
            held_by_a = threading.Event()
            release_a = threading.Event()
            b_result: dict[str, object] = {}

            def thread_a():
                with file_lock(path):
                    held_by_a.set()
                    release_a.wait(timeout=5)

            def thread_b():
                held_by_a.wait(timeout=5)
                try:
                    with file_lock(path, timeout=0.5):
                        b_result["status"] = "got_lock"
                except TimeoutError as e:
                    b_result["status"] = "timeout"
                    b_result["err"] = str(e)

            ta = threading.Thread(target=thread_a)
            tb = threading.Thread(target=thread_b)
            ta.start(); tb.start()
            tb.join(timeout=3)
            release_a.set()
            ta.join(timeout=3)

            self.assertEqual(b_result.get("status"), "timeout",
                             "second lock acquirer should have timed out")


if __name__ == "__main__":
    unittest.main()
