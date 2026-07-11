"""
provider/gemini_speech_provider.py
=====================================
Gemini Speech-to-Text Provider implementation.

Converts between the provider-independent SpeechToTextRequest/Response
models (provider.speech_provider) and the google-genai SDK's
multimodal generate_content() API, using inline audio input
(google.genai.types.Part.from_bytes) to obtain a transcript. All
google-genai interaction for speech-to-text is isolated within this
module -- the Runtime Core remains completely unaware of the SDK,
mirroring the existing boundary already established for the LLM path
(provider.gemini_provider).

Verified against the real Gemini API (google-genai 2.10.0,
gemini-2.5-flash) with a 16kHz mono PCM16 WAV: Part.from_bytes(data=...,
mime_type="audio/wav") + a text instruction Part transcribes correctly
with no modality error.

Exception normalization mirrors provider.gemini_provider's
_normalize_gemini_exception mapping, duplicated here (not imported)
because provider.gemini_provider is the frozen LLM Provider and must
not be modified or depended on by this module.

EXPORTED API:
  GeminiSpeechProvider -- SpeechToTextProvider implementation backed by
                          the google-genai SDK
"""

from typing import Optional

import httpx
from google.genai import Client
from google.genai.errors import APIError
from google.genai.types import (
    Content,
    GenerateContentConfig,
    HttpOptions,
    Part,
    ThinkingConfig,
)

from provider.errors import (
    RuntimeAuthenticationError,
    RuntimeProviderError,
    RuntimeRateLimitError,
    RuntimeServiceUnavailableError,
    RuntimeTimeoutError,
)
from provider.speech_provider import (
    SpeechToTextProvider,
    SpeechToTextRequest,
    SpeechToTextResponse,
    detect_language_from_text,
)

_AUTH_STATUSES = {"UNAUTHENTICATED", "PERMISSION_DENIED"}
_RATE_LIMIT_STATUSES = {"RESOURCE_EXHAUSTED"}
_TIMEOUT_STATUSES = {"DEADLINE_EXCEEDED"}
_UNAVAILABLE_STATUSES = {"UNAVAILABLE"}

_AUTH_CODES = {401, 403}
_RATE_LIMIT_CODES = {429}
_TIMEOUT_CODES = {504}
_UNAVAILABLE_CODES = {503}

_TRANSCRIBE_INSTRUCTION = (
    "Transcribe the following audio exactly as spoken, verbatim, in its "
    "original language. Return only the transcription text -- no "
    "commentary, labels, translation, or additional formatting."
)


def _normalize_gemini_exception(exc: Exception) -> RuntimeProviderError:
    """Mirrors provider.gemini_provider._normalize_gemini_exception."""
    if isinstance(exc, httpx.TimeoutException):
        return RuntimeTimeoutError(str(exc), provider="gemini", cause=exc)

    if isinstance(exc, APIError):
        status = (exc.status or "").upper()
        code = exc.code
        if status in _AUTH_STATUSES or code in _AUTH_CODES:
            return RuntimeAuthenticationError(str(exc), provider="gemini", cause=exc)
        if status in _RATE_LIMIT_STATUSES or code in _RATE_LIMIT_CODES:
            return RuntimeRateLimitError(str(exc), provider="gemini", cause=exc)
        if status in _TIMEOUT_STATUSES or code in _TIMEOUT_CODES:
            return RuntimeTimeoutError(str(exc), provider="gemini", cause=exc)
        if status in _UNAVAILABLE_STATUSES or code in _UNAVAILABLE_CODES:
            return RuntimeServiceUnavailableError(str(exc), provider="gemini", cause=exc)
        return RuntimeProviderError(str(exc), provider="gemini", cause=exc)

    return RuntimeProviderError(str(exc), provider="gemini", cause=exc)


class GeminiSpeechProvider(SpeechToTextProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._model = model
        http_options = HttpOptions(timeout=int(timeout * 1000)) if timeout is not None else None
        self._client = Client(api_key=api_key, http_options=http_options)

    def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextResponse:
        instruction = _TRANSCRIBE_INSTRUCTION
        if request.prompt:
            instruction = (
                f"{instruction}\n\nRecent conversation context (for "
                f"disambiguation only, do not transcribe this): {request.prompt}"
            )

        contents = [
            Content(
                role="user",
                parts=[
                    Part.from_text(text=instruction),
                    Part.from_bytes(data=request.audio_wav, mime_type="audio/wav"),
                ],
            )
        ]
        # thinking_budget=0: see provider/gemini_provider.py's
        # _to_gemini_request() for the full measured evidence (same
        # google-genai default-thinking behavior applies to any
        # generate_content() call, including this verbatim-transcription
        # one). A transcription task has no use for multi-step reasoning;
        # disabling it removes measured tail latency (real Operator E2E
        # comparison: Gemini STT observed up to 11.6s vs OpenAI's 3.6s max
        # under an identical workload) without changing any timeout.
        config = GenerateContentConfig(
            temperature=0.0, thinking_config=ThinkingConfig(thinking_budget=0)
        )

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            raise _normalize_gemini_exception(exc) from exc

        text = (response.text or "").strip()
        language = detect_language_from_text(text)
        return SpeechToTextResponse(text=text, language=language)
