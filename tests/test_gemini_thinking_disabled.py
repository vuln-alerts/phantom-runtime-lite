"""
tests/test_gemini_thinking_disabled.py
=========================================
Regression test for the Gemini-only "1011 keepalive timeout -> reconnect
-> 409" investigation: gemini-2.5-flash's default dynamic "thinking"
budget draws from the SAME max_output_tokens ceiling
phantom_runtime.py's generate_reply() sizes for a plain conversational
reply (60-150 tokens, no allowance for a reasoning model's hidden token
spend). Confirmed via direct google-genai calls (see
docs/bugs/FIX-2026-07-12-gemini-thinking-token-reply-truncation.md):
with thinking left enabled, max_output_tokens=60 was consumed almost
entirely by invisible thoughts_token_count, truncating every reply to a
handful of visible characters (finish_reason=MAX_TOKENS); with
thinking_config=ThinkingConfig(thinking_budget=0), the same prompt
returns the complete, untruncated reply, faster, not slower.

This module verifies -- offline, no network call, via a mocked
google.genai.Client -- that both provider.gemini_provider.GeminiProvider
(generate() and generate_stream(), the LLM path) and
provider.gemini_speech_provider.GeminiSpeechProvider (transcribe(), the
STT path) always pass thinking_config=ThinkingConfig(thinking_budget=0)
in the GenerateContentConfig handed to the SDK, so this fix cannot
silently regress.

Uses unittest (stdlib) + unittest.mock, consistent with the rest of this
project's test suite: pytest is not a dependency.
"""

import os
import sys
import unittest
from unittest import mock

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from google.genai.types import ThinkingConfig

from provider.gemini_provider import GeminiProvider
from provider.gemini_speech_provider import GeminiSpeechProvider
from provider.models import Message, ProviderRequest
from provider.speech_provider import SpeechToTextRequest


def _fake_generate_content_response():
    response = mock.Mock()
    response.text = "ok"
    response.candidates = [mock.Mock(finish_reason=mock.Mock(value="STOP"))]
    response.usage_metadata = mock.Mock(
        prompt_token_count=1, candidates_token_count=1, total_token_count=2
    )
    return response


class TestGeminiProviderDisablesThinking(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("provider.gemini_provider.Client")
        self.addCleanup(patcher.stop)
        self.mock_client_cls = patcher.start()
        self.mock_client = self.mock_client_cls.return_value
        self.mock_client.models.generate_content.return_value = (
            _fake_generate_content_response()
        )
        self.mock_client.models.generate_content_stream.return_value = iter([])

    def _request(self) -> ProviderRequest:
        return ProviderRequest(
            messages=[
                Message(role="system", content="sys"),
                Message(role="user", content="hello"),
            ],
            temperature=0.15,
            max_tokens=60,
        )

    def test_generate_passes_thinking_budget_zero(self):
        provider = GeminiProvider(api_key="k", model="gemini-2.5-flash", timeout=20.0)
        provider.generate(self._request())
        _, kwargs = self.mock_client.models.generate_content.call_args
        config = kwargs["config"]
        self.assertEqual(config.thinking_config, ThinkingConfig(thinking_budget=0))

    def test_generate_stream_passes_thinking_budget_zero(self):
        provider = GeminiProvider(api_key="k", model="gemini-2.5-flash", timeout=45.0)
        list(provider.generate_stream(self._request()))
        _, kwargs = self.mock_client.models.generate_content_stream.call_args
        config = kwargs["config"]
        self.assertEqual(config.thinking_config, ThinkingConfig(thinking_budget=0))


class TestGeminiSpeechProviderDisablesThinking(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("provider.gemini_speech_provider.Client")
        self.addCleanup(patcher.stop)
        self.mock_client_cls = patcher.start()
        self.mock_client = self.mock_client_cls.return_value
        self.mock_client.models.generate_content.return_value = (
            _fake_generate_content_response()
        )

    def test_transcribe_passes_thinking_budget_zero(self):
        provider = GeminiSpeechProvider(api_key="k", model="gemini-2.5-flash", timeout=30.0)
        provider.transcribe(SpeechToTextRequest(audio_wav=b"RIFF....", sample_rate=16000))
        _, kwargs = self.mock_client.models.generate_content.call_args
        config = kwargs["config"]
        self.assertEqual(config.thinking_config, ThinkingConfig(thinking_budget=0))


if __name__ == "__main__":
    unittest.main()
