"""
provider/gemini_provider.py
=============================
Gemini Provider implementation (v1.11 H1-4-2: Buffered Generation;
H1-4-3: Streaming Generation).

Converts between the provider-independent Runtime models and the
google-genai SDK. All Gemini SDK interaction is isolated within this
module — the Runtime Core remains completely unaware of the SDK.

GeminiProvider receives all configuration (api_key, model, timeout)
from its caller. It defines no Runtime-independent defaults of its
own; RuntimeConfig remains the single source of truth for these
values — mirrors OpenAIProvider's existing constraint.

Gemini SDK exceptions are normalized into the Runtime-standard error
hierarchy defined in provider.errors (H1-2), mirroring OpenAIProvider's
normalization policy (H1-3-3 / H2A-3). No Gemini SDK exception, and no
other unexpected exception raised during the SDK call, escapes
generate() or generate_stream().

--------------------------------------------------------------------
Streaming (H1-4-3)
--------------------------------------------------------------------
generate_stream() overrides ProviderInterface's additive default
(H2A-1) and emits only the provider-independent event models defined
in H2A-2 (StreamingTextDelta, StreamingCompletion, StreamingCancellation,
StreamingError). No google-genai object is ever exposed through it.

Exactly one terminal event (StreamingCompletion, StreamingCancellation,
or StreamingError) is emitted per stream, after which iteration stops.
The underlying google-genai chunk generator is closed exactly once, on
every terminal path (normal completion, explicit cancellation via
_GeminiStreamIterator.cancel(), mid-stream exception, or a failure
surfacing on the first chunk) — see _GeminiStreamIterator._close().
generate() and its existing helpers are untouched by this phase.

google.genai.models.Models.generate_content_stream() is itself a
Python generator function: calling it executes none of its body and
cannot raise — the underlying HTTP request only fires on the first
next(). Unlike OpenAIProvider.generate_stream() (whose establishment
call executes eagerly and is wrapped in its own try/except),
generate_stream() below therefore has no try/except around the
generate_content_stream() call itself — there is nothing that call can
raise. Any establishment-time failure (bad model, bad API key,
unreachable network) instead surfaces on _GeminiStreamIterator's first
__next__() call, which already normalizes any exception raised while
pulling a chunk via _normalize_gemini_exception() (shared, unmodified,
with generate()'s exception normalization). The Runtime-facing
contract is unaffected either way: generate_stream() never raises, and
a StreamingError as the first yielded event is an already-anticipated,
valid outcome under the H2A Streaming Contract.

EXPORTED API:
  GeminiProvider — ProviderInterface implementation backed by the google-genai SDK
"""

from typing import Any, Iterator, Optional

import httpx
from google.genai import Client
from google.genai.errors import APIError
from google.genai.types import Content, GenerateContentConfig, HttpOptions, Part

from provider.errors import (
    RuntimeAuthenticationError,
    RuntimeProviderError,
    RuntimeRateLimitError,
    RuntimeServiceUnavailableError,
    RuntimeTimeoutError,
)
from provider.interface import ProviderInterface
from provider.models import (
    ProviderRequest,
    ProviderResponse,
    StreamCancellationReason,
    StreamingCancellation,
    StreamingCompletion,
    StreamingError,
    StreamingEvent,
    StreamingTextDelta,
)

# google-genai reports errors via APIError.code (HTTP status) and
# APIError.status (a gRPC-style status string, e.g. "RESOURCE_EXHAUSTED").
# Unlike the OpenAI SDK, it does not expose distinct exception classes per
# failure category, so both signals are consulted for normalization.
_AUTH_STATUSES = {"UNAUTHENTICATED", "PERMISSION_DENIED"}
_RATE_LIMIT_STATUSES = {"RESOURCE_EXHAUSTED"}
_TIMEOUT_STATUSES = {"DEADLINE_EXCEEDED"}
_UNAVAILABLE_STATUSES = {"UNAVAILABLE"}

_AUTH_CODES = {401, 403}
_RATE_LIMIT_CODES = {429}
_TIMEOUT_CODES = {504}
_UNAVAILABLE_CODES = {503}


def _normalize_gemini_exception(exc: Exception) -> RuntimeProviderError:
    """
    Map a google-genai SDK exception (or any unexpected exception raised
    during a Gemini SDK call) onto the Runtime-standard error hierarchy,
    mirroring the H1-3-3 / H2A-3 OpenAI normalization policy
    (provider.openai_provider._normalize_openai_exception).

    httpx.TimeoutException is checked explicitly because google-genai's
    transport layer can raise it directly (unwrapped by APIError) when a
    request never receives an HTTP response — see google.genai._api_client.
    """
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


class _GeminiStreamIterator:
    """
    Iterator[StreamingEvent] wrapping a raw google-genai chunk generator
    (the object returned by Models.generate_content_stream()).

    Mirrors _OpenAIStreamIterator (provider.openai_provider) exactly in
    structure and guarantees. The one substantive difference is what
    "closing the underlying stream" means: google-genai's chunk
    generator is a plain Python generator, so _close() calls its
    built-in .close() method (a language guarantee on every generator
    object) rather than an SDK-authored Stream.close(). Calling .close()
    on an already-exhausted or already-closed generator is itself a
    documented no-op, so this remains safe even if invoked more than
    once — though _closed already prevents that from happening.

    Exactly one terminal event (StreamingCompletion, StreamingCancellation,
    or StreamingError) is returned, after which __next__ raises
    StopIteration on every subsequent call. The underlying generator is
    closed exactly once, on whichever terminal path is taken.
    """

    def __init__(self, gemini_stream: Any) -> None:
        self._gemini_stream = gemini_stream
        self._cancelled = False
        self._cancel_reason: Optional[StreamCancellationReason] = None
        self._terminated = False
        self._closed = False
        self._last_finish_reason: Optional[str] = None

    def __iter__(self) -> "_GeminiStreamIterator":
        return self

    def cancel(
        self,
        reason: StreamCancellationReason = StreamCancellationReason.CALLER_REQUESTED,
    ) -> None:
        """
        Request cancellation. The next __next__() call will emit
        exactly one StreamingCancellation event (with this reason) as
        the stream's terminal event, then close the underlying stream.
        """
        self._cancelled = True
        self._cancel_reason = reason

    def __next__(self) -> StreamingEvent:
        while True:
            if self._terminated:
                raise StopIteration

            if self._cancelled:
                return self._terminal(
                    StreamingCancellation(reason=self._cancel_reason)
                )

            try:
                chunk = next(self._gemini_stream)
            except StopIteration:
                return self._terminal(
                    StreamingCompletion(finish_reason=self._last_finish_reason)
                )
            except Exception as exc:
                runtime_error = _normalize_gemini_exception(exc)
                return self._terminal(
                    StreamingError(message=str(runtime_error), cause=runtime_error)
                )

            if chunk.candidates:
                raw_finish_reason = chunk.candidates[0].finish_reason
                if raw_finish_reason is not None:
                    self._last_finish_reason = raw_finish_reason.value

            text = chunk.text
            if text:
                return StreamingTextDelta(text=text)
            # Chunk carried no text (e.g. a finish-reason-only final
            # chunk, or a chunk with only non-text parts) — fetch the
            # next one.

    def _terminal(self, event: StreamingEvent) -> StreamingEvent:
        self._terminated = True
        self._close()
        return event

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._gemini_stream, "close", None)
        if callable(close):
            close()

    def __del__(self) -> None:
        # Defensive safety net: guarantees the underlying generator is
        # released even if the caller drops the iterator without fully
        # consuming or explicitly cancelling it.
        self._close()


class GeminiProvider(ProviderInterface):
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._model = model
        # HttpOptions.timeout is in milliseconds; GeminiProvider's own
        # constructor takes seconds, matching OpenAIProvider's convention.
        http_options = HttpOptions(timeout=int(timeout * 1000)) if timeout is not None else None
        self._client = Client(api_key=api_key, http_options=http_options)

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        contents, config = self._to_gemini_request(request)
        try:
            gemini_response = self._client.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            raise _normalize_gemini_exception(exc) from exc
        return self._to_provider_response(gemini_response)

    def generate_stream(self, request: ProviderRequest) -> Iterator[StreamingEvent]:
        """
        Generate a response incrementally (H1-4-3). Overrides
        ProviderInterface's additive default (H2A-1).

        generate_content_stream() is a lazy generator function (see
        module docstring) — the call below cannot raise, so unlike
        generate()/OpenAIProvider.generate_stream() there is no
        establishment-time try/except here. Any establishment failure
        surfaces on _GeminiStreamIterator's first __next__() call
        instead, normalized the same way as a mid-stream failure.
        """
        contents, config = self._to_gemini_request(request)
        gemini_stream = self._client.models.generate_content_stream(
            model=self._model,
            contents=contents,
            config=config,
        )
        return _GeminiStreamIterator(gemini_stream)

    def _to_gemini_request(
        self, request: ProviderRequest
    ) -> tuple[list[Content], GenerateContentConfig]:
        # ProviderRequest's role vocabulary (system / user / assistant) is
        # frozen by H1-2 and is OpenAI-shaped. Gemini has no "system" role in
        # its Content list — system text is instead passed as a separate
        # system_instruction — and uses "model" rather than "assistant" for
        # the assistant turn. Both translations are local to this method;
        # ProviderRequest itself is untouched.
        system_instruction: Optional[str] = None
        contents: list[Content] = []
        for message in request.messages:
            if message.role == "system":
                system_instruction = (
                    f"{system_instruction}\n{message.content}"
                    if system_instruction
                    else message.content
                )
                continue
            role = "model" if message.role == "assistant" else "user"
            contents.append(Content(role=role, parts=[Part(text=message.content)]))

        config = GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=request.temperature,
            max_output_tokens=request.max_tokens,
        )
        return contents, config

    def _to_provider_response(self, gemini_response: Any) -> ProviderResponse:
        finish_reason: Optional[str] = None
        if gemini_response.candidates:
            raw_finish_reason = gemini_response.candidates[0].finish_reason
            finish_reason = (
                raw_finish_reason.value if raw_finish_reason is not None else None
            )

        usage = gemini_response.usage_metadata
        metadata: Optional[dict[str, Any]] = None
        if usage is not None:
            metadata = {
                "prompt_tokens": usage.prompt_token_count,
                "completion_tokens": usage.candidates_token_count,
                "total_tokens": usage.total_token_count,
            }

        return ProviderResponse(
            text=gemini_response.text or "",
            finish_reason=finish_reason,
            metadata=metadata,
        )
