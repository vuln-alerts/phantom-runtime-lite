"""
provider/openai_speech_provider.py
=====================================
OpenAI Whisper Speech-to-Text Provider implementation.

Converts between the provider-independent SpeechToTextRequest/Response
models (provider.speech_provider) and the OpenAI SDK's audio
transcription API. All OpenAI SDK interaction for speech-to-text is
isolated within this module -- the Runtime Core remains completely
unaware of the SDK, mirroring the existing boundary already
established for the LLM path (provider.openai_provider).

OpenAI SDK exceptions are normalized into the Runtime-standard error
hierarchy defined in provider.errors, mirroring
provider.openai_provider's normalization policy. Unlike the LLM
Provider, retry policy is NOT implemented here -- it remains the
Runtime Core's responsibility (phantom_runtime.py's transcribe()),
exactly as before this Provider boundary was introduced.

EXPORTED API:
  OpenAISpeechProvider -- SpeechToTextProvider implementation backed by
                          the OpenAI Whisper/transcription API
"""

import io
from typing import Optional

from openai import (
    APIConnectionError,
    AuthenticationError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

from provider.errors import (
    RuntimeAuthenticationError,
    RuntimeProviderError,
    RuntimeRateLimitError,
    RuntimeTimeoutError,
)
from provider.speech_provider import (
    SpeechToTextProvider,
    SpeechToTextRequest,
    SpeechToTextResponse,
    detect_language_from_text,
)

# Model capability registry (moved from phantom_runtime.py, unchanged).
# Adding a new model = adding one entry here.
MODEL_CAPS: dict[str, dict[str, bool]] = {
    "whisper-1": {
        "verbose_json":    True,   # returns result.language field
        "supports_prompt": True,   # accepts prompt= parameter for context seeding
    },
    "gpt-4o-transcribe": {
        "verbose_json":    False,
        "supports_prompt": False,  # may 400 on some API versions
    },
    "gpt-4o-mini-transcribe": {
        "verbose_json":    False,
        "supports_prompt": False,
    },
}


def _model_cap(model: str, cap: str, default: bool = False) -> bool:
    """Lookup a capability for a model. Returns default if model is unknown."""
    return MODEL_CAPS.get(model.lower(), {}).get(cap, default)


class OpenAISpeechProvider(SpeechToTextProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._model = model
        self._client = OpenAI(api_key=api_key, timeout=timeout)

    def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextResponse:
        use_verbose = _model_cap(self._model, "verbose_json")
        use_prompt = _model_cap(self._model, "supports_prompt")
        fmt = "verbose_json" if use_verbose else "json"

        extra: dict = {}
        if use_prompt and request.prompt:
            extra["prompt"] = request.prompt

        wav_buf = io.BytesIO(request.audio_wav)
        wav_buf.name = "audio.wav"

        try:
            # Per-call timeout: 30s hard limit regardless of client default,
            # matching the pre-Provider-boundary behavior exactly.
            result = self._client.audio.transcriptions.create(
                file=wav_buf,
                model=self._model,
                response_format=fmt,
                temperature=0.0,
                timeout=30.0,
                **extra,
            )
        except AuthenticationError as exc:
            raise RuntimeAuthenticationError(str(exc), provider="openai", cause=exc) from exc
        except RateLimitError as exc:
            raise RuntimeRateLimitError(str(exc), provider="openai", cause=exc) from exc
        except APIConnectionError as exc:
            # APITimeoutError is a subclass of APIConnectionError -- both are
            # the Runtime's retryable case (see phantom_runtime.py transcribe()).
            raise RuntimeTimeoutError(str(exc), provider="openai", cause=exc) from exc
        except OpenAIError as exc:
            raise RuntimeProviderError(str(exc), provider="openai", cause=exc) from exc
        except Exception as exc:
            raise RuntimeProviderError(str(exc), provider="openai", cause=exc) from exc

        text = (result.text or "").strip()
        language = (
            (getattr(result, "language", None) or "unknown")
            if use_verbose
            else detect_language_from_text(text)
        )
        return SpeechToTextResponse(text=text, language=language)
