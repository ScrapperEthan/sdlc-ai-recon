"""Unit tests for opt-in true streaming: the SSE parser, the provider chat_stream, and the
llm facade's fallback. All local — no network / no copilot-api (the SSE body is faked)."""
import types
import unittest
from unittest import mock

from webapp import llm, config
from webapp.llm_providers import copilot_responses


def _sse(*lines):
    """Turn SSE text lines into the byte lines a urllib response iterator would yield."""
    return [(line + "\n").encode("utf-8") for line in lines]


class _FakeResp:
    def __init__(self, lines):
        self._lines = list(lines)
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


class SseParserTests(unittest.TestCase):
    def test_accumulates_events_and_honours_done(self):
        lines = _sse(
            ': heartbeat',
            'event: response.output_text.delta',
            'data: {"type":"response.output_text.delta","delta":"Hel"}',
            '',
            'data: {"type":"response.output_text.delta","delta":"lo"}',
            '',
            'data: not-json',           # tolerated: skipped, not fatal
            '',
            'data: [DONE]',
            '',
            'data: {"type":"response.completed"}',   # after DONE -> never reached
        )
        events = list(copilot_responses._iter_sse_events(iter(lines)))
        self.assertEqual([e.get("delta") for e in events], ["Hel", "lo"])


class ChatStreamTests(unittest.TestCase):
    def _run(self, lines):
        resp = _FakeResp(lines)
        with mock.patch("urllib.request.urlopen", return_value=resp):
            out = list(copilot_responses.chat_stream([{"role": "user", "content": "hi"}]))
        return out, resp

    def test_streams_deltas_then_final_from_completed(self):
        lines = _sse(
            'data: {"type":"response.output_text.delta","delta":"Hel"}',
            '',
            'data: {"type":"response.output_text.delta","delta":"lo"}',
            '',
            'data: {"type":"response.completed","response":{"output":['
            '{"type":"message","content":[{"type":"output_text","text":"Hello"}]}]}}',
            '',
        )
        out, resp = self._run(lines)
        self.assertEqual(out[0], ("delta", "Hel"))
        self.assertEqual(out[1], ("delta", "lo"))
        self.assertEqual(out[2][0], "final")
        self.assertEqual(out[2][1]["content"], "Hello")
        self.assertTrue(resp.closed)  # response is always closed

    def test_final_carries_tool_calls_from_completed_body(self):
        lines = _sse(
            'data: {"type":"response.completed","response":{"output":['
            '{"type":"function_call","call_id":"c1","name":"hubs","arguments":"{\\"top\\":5}"}]}}',
            '',
        )
        out, _ = self._run(lines)
        self.assertEqual(len(out), 1)
        kind, message = out[0]
        self.assertEqual(kind, "final")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "hubs")

    def test_raises_without_terminal_event(self):
        # deltas but no response.completed -> raise so the facade falls back to blocking chat.
        lines = _sse('data: {"type":"response.output_text.delta","delta":"x"}', '')
        with self.assertRaises(RuntimeError):
            self._run(lines)


class FacadeFallbackTests(unittest.TestCase):
    def _fake_provider(self, chat_result=None, chat_stream=None):
        ns = types.SimpleNamespace()
        ns.chat = mock.Mock(return_value=chat_result or {"role": "assistant", "content": "BLOCK"})
        if chat_stream is not None:
            ns.chat_stream = chat_stream
        return ns

    def test_off_yields_single_blocking_final(self):
        provider = self._fake_provider()
        with mock.patch.object(config, "LLM_MOCK", False), \
             mock.patch.object(config, "LLM_STREAM", False), \
             mock.patch.object(llm, "_provider_module", return_value=provider):
            out = list(llm.chat_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, [("final", {"role": "assistant", "content": "BLOCK"})])
        provider.chat.assert_called_once()

    def test_stream_error_before_any_delta_falls_back_cleanly(self):
        def boom(*_a, **_k):
            raise RuntimeError("no SSE")
            yield  # pragma: no cover — make it a generator
        provider = self._fake_provider(chat_stream=boom)
        with mock.patch.object(config, "LLM_MOCK", False), \
             mock.patch.object(config, "LLM_STREAM", True), \
             mock.patch.object(llm, "_provider_module", return_value=provider):
            out = list(llm.chat_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, [("final", {"role": "assistant", "content": "BLOCK"})])
        provider.chat.assert_called_once()

    def test_stream_happy_path_passes_through(self):
        def ok(*_a, **_k):
            yield ("delta", "a")
            yield ("final", {"role": "assistant", "content": "a"})
        provider = self._fake_provider(chat_stream=ok)
        with mock.patch.object(config, "LLM_MOCK", False), \
             mock.patch.object(config, "LLM_STREAM", True), \
             mock.patch.object(llm, "_provider_module", return_value=provider):
            out = list(llm.chat_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, [("delta", "a"), ("final", {"role": "assistant", "content": "a"})])
        provider.chat.assert_not_called()

    def test_mock_mode_yields_final(self):
        with mock.patch.object(config, "LLM_MOCK", True):
            out = list(llm.chat_stream([{"role": "user", "content": "hi"}], tools=None))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "final")


if __name__ == "__main__":
    unittest.main()
