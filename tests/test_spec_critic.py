"""Tests for the spec + completeness-critic pipeline.

The LLM-driven steps (`generate_spec`, `_llm_review_ambiguous`) are tested
via mocked `chat()` responses — we control what the model "returns" and
verify the parser handles each shape (clean JSON, fenced JSON, garbage,
missing keys). The static prefilter and coverage scoring run end-to-end
against synthetic workspaces.
"""

from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from cadillac.critic import (
    MissingFeature,
    audit_completeness,
    completeness_score,
    format_for_iterate,
)
from cadillac.languages import python_language
from cadillac.spec import Spec, Story, coverage_report, generate_spec


def _mkfile(ws: str, rel: str, content: str) -> None:
    full = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(full) or ws, exist_ok=True)
    with open(full, "w") as f:
        f.write(textwrap.dedent(content))


class _StubCfg:
    api_url = "http://localhost:8000/v1"
    model = "stub"
    context_window = 65536
    max_context_tokens = 40000
    stream = False
    enable_thinking = False
    api_key = None
    rate_limit = 0.0


# ── Story / Spec dataclass ────────────────────────────────────────────────────


class TestStoryRoundtrip(unittest.TestCase):
    def test_basic_roundtrip(self):
        s = Story(id="S01", title="t",
                  acceptance=("a", "b"), priority="must", category="auth")
        self.assertEqual(s, Story.from_dict(s.to_dict()))

    def test_invalid_priority_defaults_to_must(self):
        s = Story.from_dict({"id": "S01", "title": "t",
                              "priority": "garbage"})
        self.assertEqual(s.priority, "must")

    def test_string_acceptance_becomes_tuple(self):
        s = Story.from_dict({"id": "S01", "title": "t",
                              "acceptance": "single line"})
        self.assertEqual(s.acceptance, ("single line",))


class TestSpecPersist(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as ws:
            s1 = Spec(task="x", stories=[
                Story(id="S01", title="hello", acceptance=("a",)),
            ])
            s1.save(ws)
            s2 = Spec.load(ws)
            self.assertIsNotNone(s2)
            self.assertEqual(len(s2.stories), 1)
            self.assertEqual(s2.stories[0].id, "S01")

    def test_load_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertIsNone(Spec.load(ws))

    def test_load_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "spec.json"), "w") as f:
                f.write("not json")
            self.assertIsNone(Spec.load(ws))


class TestSpecPromptBlock(unittest.TestCase):
    def test_must_stories_survive_truncation(self):
        stories = [
            Story(id=f"S{i:02d}", title=f"title {i}",
                   acceptance=(f"acceptance {i}",),
                   priority="should" if i > 1 else "must")
            for i in range(30)
        ]
        s = Spec(task="t", stories=stories)
        block = s.to_prompt_block(max_chars=400)
        self.assertIn("S00", block)  # must
        self.assertIn("S01", block)  # must
        # Truncation marker should mention dropped stories
        self.assertIn("omitted", block)


# ── generate_spec (mocked LLM) ────────────────────────────────────────────────


class TestGenerateSpec(unittest.TestCase):
    def _fake_response(self, content: str) -> dict:
        return {"role": "assistant", "content": content, "tool_calls": []}

    def test_parses_clean_json(self):
        payload = json.dumps({
            "stories": [
                {"id": "S01", "title": "signup",
                 "acceptance": ["POST /signup → 201"],
                 "priority": "must", "category": "auth"},
                {"id": "S02", "title": "logout",
                 "acceptance": ["POST /logout clears session"],
                 "priority": "should", "category": "auth"},
            ]
        })
        with patch("cadillac.engine.chat",
                    return_value=self._fake_response(payload)):
            spec = generate_spec("a thing", _StubCfg(), python_language())
        self.assertEqual(len(spec.stories), 2)
        ids = {s.id for s in spec.stories}
        self.assertEqual(ids, {"S01", "S02"})

    def test_strips_fences(self):
        payload = "```json\n" + json.dumps({"stories": [
            {"id": "S01", "title": "x", "acceptance": ["a"]},
        ]}) + "\n```"
        with patch("cadillac.engine.chat",
                    return_value=self._fake_response(payload)):
            spec = generate_spec("t", _StubCfg(), python_language())
        self.assertEqual(len(spec.stories), 1)

    def test_garbage_returns_empty_spec(self):
        with patch("cadillac.engine.chat",
                    return_value=self._fake_response("not json at all")):
            spec = generate_spec("t", _StubCfg(), python_language())
        self.assertTrue(spec.is_empty())

    def test_dedups_repeated_ids(self):
        payload = json.dumps({"stories": [
            {"id": "S01", "title": "x", "acceptance": ["a"]},
            {"id": "S01", "title": "DUPE", "acceptance": ["b"]},
        ]})
        with patch("cadillac.engine.chat",
                    return_value=self._fake_response(payload)):
            spec = generate_spec("t", _StubCfg(), python_language())
        self.assertEqual(len(spec.stories), 1)
        self.assertEqual(spec.stories[0].title, "x")

    def test_llm_unreachable_returns_empty_spec(self):
        with patch("cadillac.engine.chat", side_effect=RuntimeError("nope")):
            spec = generate_spec("t", _StubCfg(), python_language())
        self.assertTrue(spec.is_empty())


# ── Coverage prefilter ────────────────────────────────────────────────────────


class TestCoverageReport(unittest.TestCase):
    def test_keywords_present_means_covered(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "app.py",
                    "def signup(email, password):\n    pass\n")
            spec = Spec(task="t", stories=[
                Story(id="S01", title="user signup with email and password",
                       acceptance=("POST /signup returns 201",)),
            ])
            rpt = coverage_report(spec, ws)
            self.assertIn("S01", rpt["covered"])
            self.assertEqual(rpt["score"], 1.0)

    def test_keywords_absent_means_missing(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "app.py", "def add(a, b):\n    return a + b\n")
            spec = Spec(task="t", stories=[
                Story(id="S01",
                       title="user can log out and clear session cookies",
                       acceptance=("POST /logout clears session",)),
            ])
            rpt = coverage_report(spec, ws)
            self.assertIn("S01", rpt["missing"])

    def test_empty_spec_is_full_coverage(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(coverage_report(Spec(task="t"), ws)["score"], 1.0)


# ── Critic orchestrator ───────────────────────────────────────────────────────


class TestAuditCompleteness(unittest.TestCase):
    def test_static_prefilter_only(self):
        """LLM disabled → critic returns only static-prefilter misses."""
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "app.py", "def add(a, b):\n    return a + b\n")
            spec = Spec(task="t", stories=[
                Story(id="S01",
                       title="user can log out and clear session cookies",
                       acceptance=("POST /logout",),
                       priority="must"),
                Story(id="S02",
                       title="add two numbers",
                       acceptance=("add(1,2) returns 3",),
                       priority="should"),
            ])
            missing = audit_completeness(
                spec, ws, manifest_summary="app.py",
                lang=python_language(), cfg=_StubCfg(),
                llm_review=False,
            )
            ids = {m.story_id for m in missing}
            self.assertIn("S01", ids)
            self.assertNotIn("S02", ids)
            # S01 was must-priority → high severity, confidence 1.0
            entry = [m for m in missing if m.story_id == "S01"][0]
            self.assertEqual(entry.severity, "high")
            self.assertEqual(entry.confidence, 1.0)

    def test_llm_layer_adds_findings(self):
        """LLM flags a story whose keywords ARE present but unrelated.

        Static prefilter passes (keywords hit), so the story enters LLM
        review. The mocked LLM declares it missing, with a suggested file
        path that should land on the resulting MissingFeature.
        """
        with tempfile.TemporaryDirectory() as ws:
            # Source mentions ALL the story keywords so static prefilter
            # marks the story as 'covered' — forcing it into LLM review.
            _mkfile(ws, "app.py", '''\
                # password reset flow is incomplete: reset, email, requests, sends
                def signup(): pass
            ''')
            spec = Spec(task="t", stories=[
                Story(id="S01",
                       title="requests password reset email",
                       acceptance=("sends reset email",),
                       priority="must"),
            ])
            # Confirm static prefilter sees it as covered, not missing
            prereport = coverage_report(spec, ws)
            self.assertIn("S01", prereport["covered"],
                          "test premise: keywords must be present statically")

            llm_payload = json.dumps({"missing": [
                {"story_id": "S01", "status": "missing",
                 "why": "no /reset route", "confidence": 0.9,
                 "suggested_files": ["routes/auth.py"]},
            ]})
            with patch("cadillac.engine.chat",
                        return_value={"role": "assistant",
                                       "content": llm_payload,
                                       "tool_calls": []}):
                missing = audit_completeness(
                    spec, ws, manifest_summary="app.py",
                    lang=python_language(), cfg=_StubCfg(),
                    llm_review=True,
                )
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0].story_id, "S01")
            self.assertIn("routes/auth.py", missing[0].suggested_files)

    def test_ignores_hallucinated_story_ids(self):
        with tempfile.TemporaryDirectory() as ws:
            _mkfile(ws, "app.py",
                    "def signup(): pass\ndef login(): pass\n")
            spec = Spec(task="t", stories=[
                Story(id="S01", title="signup", acceptance=("a",)),
            ])
            llm_payload = json.dumps({"missing": [
                {"story_id": "S99", "status": "missing",
                 "why": "hallucinated", "confidence": 0.9},
            ]})
            with patch("cadillac.engine.chat",
                        return_value={"role": "assistant",
                                       "content": llm_payload,
                                       "tool_calls": []}):
                missing = audit_completeness(
                    spec, ws, manifest_summary="",
                    lang=python_language(), cfg=_StubCfg(),
                    llm_review=True,
                )
            self.assertEqual(missing, [])

    def test_empty_spec_returns_empty(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(
                audit_completeness(Spec(task="t"), ws, "",
                                    python_language(), _StubCfg(),
                                    llm_review=False),
                [],
            )


# ── Score + format ────────────────────────────────────────────────────────────


class TestCompletenessScore(unittest.TestCase):
    def test_empty_missing_full_score(self):
        s = Spec(task="t", stories=[Story(id="S01", title="x",
                                            acceptance=("a",))])
        self.assertEqual(completeness_score(s, []), 1.0)

    def test_all_musts_missing_floor(self):
        s = Spec(task="t", stories=[
            Story(id="S01", title="x", acceptance=("a",), priority="must"),
        ])
        m = [MissingFeature(story_id="S01", title="x", priority="must",
                             severity="high", confidence=1.0,
                             why_missing="")]
        self.assertEqual(completeness_score(s, m), 0.0)

    def test_should_misses_partial(self):
        s = Spec(task="t", stories=[
            Story(id="S01", title="x", acceptance=("a",), priority="must"),
            Story(id="S02", title="y", acceptance=("a",), priority="should"),
        ])
        m = [MissingFeature(story_id="S02", title="y", priority="should",
                             severity="medium", confidence=1.0,
                             why_missing="")]
        score = completeness_score(s, m)
        self.assertGreater(score, 0.5)
        self.assertLess(score, 1.0)


class TestFormatForIterate(unittest.TestCase):
    def test_empty_list_empty_string(self):
        self.assertEqual(format_for_iterate([]), "")

    def test_caps_at_top_n(self):
        many = [MissingFeature(story_id=f"S{i:02d}", title=f"t{i}",
                                priority="must", severity="high",
                                confidence=1.0, why_missing="x")
                for i in range(15)]
        out = format_for_iterate(many, top_n=5)
        self.assertIn("(+ 10 more", out)
        self.assertEqual(out.count("###"), 5)


if __name__ == "__main__":
    unittest.main()
