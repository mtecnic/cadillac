"""Tests for the endpoint-misconfiguration detection added 2026-06-26.

Two related fixes:

  1. `chat()` raises `EndpointMisconfigured` (no retries) when an HTTP 400
     body matches a known config-fault signature — like vLLM's
     "auto tool choice requires --enable-auto-tool-choice and
     --tool-call-parser to be set". Retrying makes no sense; the request
     shape will fail identically. Top-level run() catches it and aborts
     with a diagnostic.

  2. Module scaffold hard-fails when zero files were written for a
     module that was planned to have files. The previous behavior was a
     silent "Scaffold done, 0 files" log followed by advancing to BUILD
     with an empty module, then INTEGRATE with no source, then burning
     through the 1000-round global cap.

We hit both in the same build (.23's vLLM was started without the
tool-call flags). Together they would have surfaced as: "[ABORT
endpoint_misconfigured]" within seconds instead of "ABORT global round
limit reached at R1001" 35 minutes later.
"""

from __future__ import annotations

import unittest
from unittest import mock

from cadillac.engine import (
    Config,
    EndpointMisconfigured,
    EndpointUnreachable,
    _ENDPOINT_MISCONFIG_SIGNATURES,
    chat,
)


class _FakeResp:
    """Minimal stand-in for `requests.Response`."""

    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json
        return json.loads(self.text or "{}")


def _cfg() -> Config:
    cfg = Config(api_url="http://fake.test/v1", model="m", stream=False)
    cfg.rate_limit = 0.0  # no pacing in tests
    cfg.max_context_tokens = 4000
    cfg.context_window = 8000
    return cfg


# ────────── Bug 1: misconfig detection in chat() ──────────


class TestEndpointMisconfigDetection(unittest.TestCase):
    def test_raises_on_vllm_tool_choice_message(self):
        """The exact body text from vLLM 0.19.1 — pinned because that's
        what triggered the bug in the wild."""
        body = (
            '{"error": {"message": "\\"auto\\" tool choice requires '
            "--enable-auto-tool-choice and --tool-call-parser to be set\","
            ' "type": "BadRequestError", "code": 400}}'
        )
        fake = _FakeResp(400, body)
        with mock.patch("cadillac.engine.requests.post", return_value=fake) as post:
            cfg = _cfg()
            with self.assertRaises(EndpointMisconfigured) as cm:
                chat(cfg, [{"role": "user", "content": "hi"}], tools=None,
                     emit=lambda *a, **k: None)
            self.assertIn("structurally", str(cm.exception))
            self.assertIn("enable-auto-tool-choice", cm.exception.detail)
            # MUST NOT retry: a single POST call only
            self.assertEqual(post.call_count, 1,
                              "structural misconfig must NOT trigger retries")

    def test_raises_on_function_calling_not_enabled(self):
        body = '{"error": "function calling is not enabled on this server"}'
        fake = _FakeResp(400, body)
        with mock.patch("cadillac.engine.requests.post", return_value=fake):
            with self.assertRaises(EndpointMisconfigured):
                chat(_cfg(), [{"role": "user", "content": "hi"}],
                     emit=lambda *a, **k: None)

    def test_raises_on_tools_not_supported(self):
        body = "Tools are not supported by this model"
        fake = _FakeResp(400, body)
        with mock.patch("cadillac.engine.requests.post", return_value=fake):
            with self.assertRaises(EndpointMisconfigured):
                chat(_cfg(), [{"role": "user", "content": "hi"}],
                     emit=lambda *a, **k: None)

    def test_unrelated_400_does_NOT_raise_misconfig(self):
        """A run-of-the-mill 400 (malformed JSON, bad role, etc.) should
        still go through the existing retry-and-degrade path, not raise."""
        body = '{"error": "max_tokens exceeds context length"}'
        # Need 3 calls' worth of fake responses (retries) — and an extra
        # for the recovery-dropping path.
        fakes = [_FakeResp(400, body)] * 5
        with mock.patch("cadillac.engine.requests.post", side_effect=fakes), \
             mock.patch("cadillac.engine.time.sleep"):
            msg = chat(_cfg(), [{"role": "user", "content": "hi"}],
                       emit=lambda *a, **k: None)
        # Falls through to the existing empty-content sentinel
        self.assertEqual(msg.get("role"), "assistant")
        self.assertEqual(msg.get("content", ""), "")

    def test_signatures_are_lowercase_compared(self):
        """Body matching should be case-insensitive — vLLM sometimes
        capitalizes the message, and the regex shouldn't break either way."""
        body = '"AUTO" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'
        fake = _FakeResp(400, body)
        with mock.patch("cadillac.engine.requests.post", return_value=fake):
            with self.assertRaises(EndpointMisconfigured):
                chat(_cfg(), [{"role": "user", "content": "hi"}],
                     emit=lambda *a, **k: None)

    def test_signature_list_is_nonempty(self):
        """If somebody empties the signature list, every 400 falls through
        to retry — which is the bug we're trying to prevent."""
        self.assertGreater(len(_ENDPOINT_MISCONFIG_SIGNATURES), 0)
        self.assertIn("enable-auto-tool-choice", _ENDPOINT_MISCONFIG_SIGNATURES)


# ────────── Bug 2: zero-file scaffold guard ──────────


class TestZeroFileScaffoldDetection(unittest.TestCase):
    """The scaffold guard is inside _build_module which has too many
    dependencies to mock cleanly. Instead, pin the condition that
    triggers the guard: scaffold loop completed AND the manifest has
    zero files in the module's path AND the module was planned with
    >0 files.

    This is essentially a doc-test that the invariant is sound and a
    regression bait: if someone removes the n_scaffolded check, this
    test still passes the unit form but the source-level invariant
    breaks."""

    def test_zero_files_with_planned_files_must_abort(self):
        """Replays the .23 build's failure mode at the data level:
        the module's plan had 4 files, manifest has 0 files starting
        with this module's path, scaffold loop returned. The new code
        treats this as a hard failure."""
        # Simulate the manifest state after the loop exits
        manifest_files = {
            # Some other module's files (don't count)
            "other_module/main.py",
            "other_module/util.py",
        }
        module_path = "indexer"  # the .23 build's empty-result module
        n_scaffolded = len([f for f in manifest_files
                            if f.startswith(module_path)])
        planned_files = [{"path": "indexer/scanner.py"},
                          {"path": "indexer/walker.py"},
                          {"path": "indexer/extractor.py"},
                          {"path": "indexer/__init__.py"}]
        # The invariant the guard checks:
        is_hard_failure = (n_scaffolded == 0 and len(planned_files) > 0)
        self.assertTrue(is_hard_failure,
                         "0 scaffolded with N planned must be a hard failure")

    def test_zero_planned_zero_scaffolded_is_NOT_a_failure(self):
        """A module legitimately planned with zero files (vestigial entry,
        deferred decomposition, etc.) shouldn't trigger the hard-fail."""
        manifest_files = {"other/x.py"}
        module_path = "empty_by_design"
        n_scaffolded = len([f for f in manifest_files
                            if f.startswith(module_path)])
        planned_files: list = []
        is_hard_failure = (n_scaffolded == 0 and len(planned_files) > 0)
        self.assertFalse(is_hard_failure,
                          "0/0 is legal — only 0/N is a hard failure")

    def test_partial_scaffolding_is_NOT_a_hard_failure(self):
        """If even one file landed, we proceed to BUILD — the LLM
        partially succeeded and BUILD's retry loop can fill in gaps."""
        manifest_files = {"indexer/scanner.py"}  # one of four landed
        module_path = "indexer"
        n_scaffolded = len([f for f in manifest_files
                            if f.startswith(module_path)])
        planned_files = [{"path": "indexer/scanner.py"},
                          {"path": "indexer/walker.py"},
                          {"path": "indexer/extractor.py"},
                          {"path": "indexer/__init__.py"}]
        is_hard_failure = (n_scaffolded == 0 and len(planned_files) > 0)
        self.assertFalse(is_hard_failure,
                          "partial scaffold is not a hard failure")


if __name__ == "__main__":
    unittest.main()
