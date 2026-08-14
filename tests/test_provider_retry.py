"""Tests for provider error classification and the empty-response terminal.

Before this, `chat()` did 3 attempts x flat `time.sleep(3)` with no 429
handling, no Retry-After, and no jitter — then returned
`{"role": "assistant", "content": ""}` on exhaustion. The phase loop consumed
that sentinel as an ordinary empty round, so a dead, throttled, or
quota-exhausted endpoint drained the whole round budget while looking like a
model that kept declining to act.

Four properties are pinned here:
  1. transient (429 / 5xx / network) is retried; terminal (4xx / quota) is not
  2. Retry-After is honored and clamped
  3. backoff is exponential WITH jitter (parallel module waves must not retry
     in lockstep against one local vLLM)
  4. an empty completion is a distinct terminal, never a silent empty round
"""

import unittest
from unittest import mock

import requests

from cadillac.engine import (
    RETRY_BASE_DELAY,
    RETRY_MAX_ATTEMPTS,
    RETRY_MAX_DELAY,
    TRANSIENT_NETWORK_ERROR,
    TRANSIENT_RATE_LIMITED,
    TRANSIENT_SERVER_ERROR,
    EmptyResponse,
    EndpointUnreachable,
    ProviderFailure,
    QuotaExhausted,
    _is_empty_completion,
    backoff_delay,
    chat,
    classify_transient,
    is_quota_exhausted,
    parse_retry_after,
)


class TestClassifyTransient(unittest.TestCase):
    def test_429_is_rate_limited(self):
        self.assertEqual(classify_transient(status_code=429), TRANSIENT_RATE_LIMITED)

    def test_5xx_is_server_error(self):
        for code in (500, 502, 503, 504, 529):
            self.assertEqual(classify_transient(status_code=code), TRANSIENT_SERVER_ERROR, code)

    def test_4xx_is_terminal(self):
        for code in (400, 401, 403, 404, 422):
            self.assertIsNone(classify_transient(status_code=code), code)

    def test_quota_429_is_terminal(self):
        """An exhausted quota carries a 429 but must NOT be retried."""
        self.assertIsNone(
            classify_transient(status_code=429, body_text='{"error":{"code":"insufficient_quota"}}')
        )

    def test_bare_429_stays_retryable(self):
        """False-negative safe: an ambiguous 429 is throttling, not quota."""
        self.assertEqual(
            classify_transient(status_code=429, body_text="Too Many Requests"),
            TRANSIENT_RATE_LIMITED,
        )

    def test_connection_error_is_network(self):
        exc = requests.exceptions.ConnectionError("refused")
        self.assertEqual(classify_transient(exc=exc), TRANSIENT_NETWORK_ERROR)

    def test_timeout_is_server_error(self):
        exc = requests.exceptions.Timeout("timed out")
        self.assertEqual(classify_transient(exc=exc), TRANSIENT_SERVER_ERROR)

    def test_chunked_encoding_is_server_error(self):
        exc = requests.exceptions.ChunkedEncodingError("truncated")
        self.assertEqual(classify_transient(exc=exc), TRANSIENT_SERVER_ERROR)

    def test_nothing_supplied_is_none(self):
        self.assertIsNone(classify_transient())


class TestQuotaDetection(unittest.TestCase):
    def test_insufficient_quota(self):
        self.assertTrue(is_quota_exhausted(429, '{"code": "insufficient_quota"}'))

    def test_resource_exhausted(self):
        self.assertTrue(is_quota_exhausted(429, "RESOURCE_EXHAUSTED"))

    def test_openai_prose(self):
        self.assertTrue(is_quota_exhausted(429, "You exceeded your current quota, please check"))

    def test_403_with_quota_signal(self):
        self.assertTrue(is_quota_exhausted(403, "billing_quota_exceeded"))

    def test_bare_429_is_not_quota(self):
        self.assertFalse(is_quota_exhausted(429, "Too Many Requests"))

    def test_bare_403_is_not_quota(self):
        self.assertFalse(is_quota_exhausted(403, "Forbidden"))

    def test_500_is_never_quota(self):
        self.assertFalse(is_quota_exhausted(500, "insufficient_quota"))


class TestRetryAfter(unittest.TestCase):
    def test_parses_delta_seconds(self):
        self.assertEqual(parse_retry_after({"Retry-After": "2"}), 2.0)

    def test_case_insensitive(self):
        self.assertEqual(parse_retry_after({"retry-after": "1.5"}), 1.5)

    def test_clamped_to_max(self):
        self.assertEqual(parse_retry_after({"Retry-After": "9999"}), RETRY_MAX_DELAY)

    def test_http_date_ignored(self):
        self.assertIsNone(parse_retry_after({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))

    def test_negative_ignored(self):
        self.assertIsNone(parse_retry_after({"Retry-After": "-5"}))

    def test_absent_is_none(self):
        self.assertIsNone(parse_retry_after({}))
        self.assertIsNone(parse_retry_after(None))


class TestBackoff(unittest.TestCase):
    def test_honors_retry_after(self):
        self.assertEqual(backoff_delay(1, retry_after=2.0), 2.0)

    def test_retry_after_clamped(self):
        self.assertEqual(backoff_delay(1, retry_after=1000.0), RETRY_MAX_DELAY)

    def test_grows_exponentially(self):
        """With jitter pinned high, successive attempts must increase."""
        delays = [backoff_delay(n, rng=lambda: 1.0) for n in (1, 2, 3)]
        self.assertEqual(delays, sorted(delays))
        self.assertLess(delays[0], delays[-1])

    def test_capped_at_max(self):
        self.assertLessEqual(backoff_delay(20, rng=lambda: 1.0), RETRY_MAX_DELAY)

    def test_jitter_spreads_delays(self):
        """The whole point: parallel module waves must not retry in lockstep."""
        low = backoff_delay(3, rng=lambda: 0.0)
        high = backoff_delay(3, rng=lambda: 1.0)
        self.assertLess(low, high)

    def test_never_zero(self):
        self.assertGreater(backoff_delay(1, rng=lambda: 0.0), 0)

    def test_first_attempt_near_base(self):
        self.assertLessEqual(backoff_delay(1, rng=lambda: 1.0), RETRY_BASE_DELAY)


class TestEmptyCompletionDetection(unittest.TestCase):
    def test_blank_content_no_tools_is_empty(self):
        self.assertTrue(_is_empty_completion({"role": "assistant", "content": ""}))

    def test_none_content_no_tools_is_empty(self):
        self.assertTrue(_is_empty_completion({"role": "assistant", "content": None}))

    def test_whitespace_only_is_empty(self):
        self.assertTrue(_is_empty_completion({"role": "assistant", "content": "  \n "}))

    def test_text_is_not_empty(self):
        self.assertFalse(_is_empty_completion({"role": "assistant", "content": "hi"}))

    def test_tool_call_alone_is_not_empty(self):
        msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}
        self.assertFalse(_is_empty_completion(msg))

    def test_non_dict_is_empty(self):
        self.assertTrue(_is_empty_completion(None))


def _cfg():
    from cadillac.engine import Config
    return Config(api_url="http://test.invalid/v1", model="m", stream=False, rate_limit=0.0)


def _response(status, body=None, headers=None, text=""):
    resp = mock.Mock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = text
    resp.json.return_value = body or {}
    return resp


def _ok(content="done"):
    return _response(200, {"choices": [{"message": {"role": "assistant", "content": content},
                                        "finish_reason": "stop"}], "usage": {}})


def _empty():
    return _response(200, {"choices": [{"message": {"role": "assistant", "content": ""},
                                        "finish_reason": "stop"}], "usage": {}})


class TestChatRetryBehaviour(unittest.TestCase):
    """End-to-end through chat() with the transport mocked and sleep stubbed."""

    def setUp(self):
        self._sleep = mock.patch("cadillac.engine.time.sleep").start()
        self.addCleanup(mock.patch.stopall)

    def _run(self, responses=None, side_effect=None):
        with mock.patch("cadillac.engine.requests.post") as post:
            if side_effect is not None:
                post.side_effect = side_effect
            else:
                post.side_effect = responses
            return chat(_cfg(), [{"role": "user", "content": "hi"}], tools=[]), post

    def test_success_first_try(self):
        msg, post = self._run([_ok()])
        self.assertEqual(msg["content"], "done")
        self.assertEqual(post.call_count, 1)

    def test_429_retried_then_succeeds(self):
        msg, post = self._run([_response(429, text="Too Many Requests"), _ok()])
        self.assertEqual(msg["content"], "done")
        self.assertEqual(post.call_count, 2)

    def test_503_retried_then_succeeds(self):
        msg, post = self._run([_response(503, text="unavailable"), _ok()])
        self.assertEqual(msg["content"], "done")
        self.assertEqual(post.call_count, 2)

    def test_401_not_retried(self):
        with self.assertRaises(ProviderFailure):
            self._run([_response(401, text="bad key")] * RETRY_MAX_ATTEMPTS)

    def test_401_costs_one_attempt(self):
        with mock.patch("cadillac.engine.requests.post") as post:
            post.return_value = _response(401, text="bad key")
            with self.assertRaises(ProviderFailure):
                chat(_cfg(), [{"role": "user", "content": "hi"}], tools=[])
            self.assertEqual(post.call_count, 1, "a terminal 4xx must not consume retries")

    def test_quota_raises_quota_exhausted(self):
        with mock.patch("cadillac.engine.requests.post") as post:
            post.return_value = _response(429, text='{"error":{"code":"insufficient_quota"}}')
            with self.assertRaises(QuotaExhausted):
                chat(_cfg(), [{"role": "user", "content": "hi"}], tools=[])
            self.assertEqual(post.call_count, 1, "quota must not be retried")

    def test_retry_after_header_used(self):
        self._run([_response(429, headers={"Retry-After": "2"}, text="slow down"), _ok()])
        self.assertAlmostEqual(self._sleep.call_args_list[0].args[0], 2.0, places=3)

    def test_exhausted_transient_raises(self):
        with self.assertRaises(ProviderFailure):
            self._run([_response(503, text="down")] * RETRY_MAX_ATTEMPTS)

    def test_connection_errors_raise_endpoint_unreachable(self):
        with self.assertRaises(EndpointUnreachable):
            self._run(side_effect=requests.exceptions.ConnectionError("refused"))

    def test_timeout_exhausted_raises_provider_failure(self):
        with self.assertRaises(ProviderFailure):
            self._run(side_effect=requests.exceptions.Timeout("slow"))


class TestEmptyResponseTerminal(unittest.TestCase):
    def setUp(self):
        mock.patch("cadillac.engine.time.sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_persistent_empty_raises(self):
        """The core regression: this used to return a sentinel that the phase
        loop counted as a normal round."""
        with mock.patch("cadillac.engine.requests.post") as post:
            post.return_value = _empty()
            with self.assertRaises(EmptyResponse):
                chat(_cfg(), [{"role": "user", "content": "hi"}], tools=[])

    def test_empty_then_content_recovers(self):
        with mock.patch("cadillac.engine.requests.post") as post:
            post.side_effect = [_empty(), _ok("recovered")]
            msg = chat(_cfg(), [{"role": "user", "content": "hi"}], tools=[])
            self.assertEqual(msg["content"], "recovered")

    def test_empty_retries_are_bounded(self):
        with mock.patch("cadillac.engine.requests.post") as post:
            post.return_value = _empty()
            with self.assertRaises(EmptyResponse):
                chat(_cfg(), [{"role": "user", "content": "hi"}], tools=[])
            self.assertLessEqual(post.call_count, RETRY_MAX_ATTEMPTS + 1)

    def test_never_returns_empty_sentinel(self):
        """No path may return the old `{"content": ""}` message."""
        with mock.patch("cadillac.engine.requests.post") as post:
            post.return_value = _empty()
            try:
                msg = chat(_cfg(), [{"role": "user", "content": "hi"}], tools=[])
            except EmptyResponse:
                return
            self.fail(f"returned a sentinel instead of raising: {msg!r}")


class TestExceptionHierarchy(unittest.TestCase):
    """One handler in run() must be able to catch every terminal outcome."""

    def test_all_terminals_are_provider_failures(self):
        from cadillac.engine import EndpointMisconfigured
        for cls in (EndpointUnreachable, EndpointMisconfigured, QuotaExhausted, EmptyResponse):
            self.assertTrue(issubclass(cls, ProviderFailure), cls.__name__)

    def test_provider_failure_is_runtime_error(self):
        """Pre-existing `except RuntimeError` handlers keep working."""
        self.assertTrue(issubclass(ProviderFailure, RuntimeError))


if __name__ == "__main__":
    unittest.main()
