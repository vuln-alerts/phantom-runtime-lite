"""
provider/openai_provider.py
=============================
OpenAI Provider implementation (H1-3-2: Request / Response Conversion;
H2A-3: OpenAI Streaming Provider).

Converts between the provider-independent Runtime models and the OpenAI
SDK. All OpenAI SDK interaction is isolated within this module — the
Runtime Core remains completely unaware of the SDK.

OpenAIProvider receives all configuration (api_key, model, timeout) from
its caller. It defines no Runtime-independent defaults of its own;
RuntimeConfig remains the single source of truth for these values.

The OpenAI SDK invocation implemented here is exercised only for
isolated provider validation during H1-3-2 / H1-3-3 / H2A-3 / H2A-4.
The Runtime Core continues to bypass this provider entirely. Runtime
integration is deferred to H1-3-4 / H2A-5.

OpenAI SDK exceptions are normalized into the Runtime-standard error
hierarchy defined in provider.errors (H1-2). No OpenAI SDK exception,
and no other unexpected exception raised during the SDK call, escapes
generate() or generate_stream().

--------------------------------------------------------------------
Streaming (H2A-3)
--------------------------------------------------------------------
generate_stream() overrides ProviderInterface's additive default
(H2A-1) and emits only the provider-independent event models defined
in H2A-2 (StreamingTextDelta, StreamingCompletion, StreamingCancellation,
StreamingError). No OpenAI SDK object (ChatCompletionChunk, Stream,
etc.) is ever exposed through it.

Exactly one terminal event (StreamingCompletion, StreamingCancellation,
or StreamingError) is emitted per stream, after which iteration stops.
The underlying OpenAI Stream object is closed exactly once, on every
terminal path (normal completion, explicit cancellation via
_OpenAIStreamIterator.cancel(), mid-stream exception, or establishment
exception) — see _OpenAIStreamIterator._close(). generate() and its
existing helpers are untouched by this phase.

EXPORTED API:
  OpenAIProvider — ProviderInterface implementation backed by the OpenAI SDK
"""

from typing import Any, Iterator, Optional

from openai import (
    APITimeoutError,
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


def _normalize_openai_exception(exc: Exception) -> RuntimeProviderError:
    """
    Map an OpenAI SDK exception (or any unexpected exception raised
    during an OpenAI SDK call) onto the Runtime-standard error
    hierarchy, reusing the H1-3-3 exception normalization policy.

    Used only by the streaming path (generate_stream /
    _OpenAIStreamIterator). generate()'s existing inline try/except is
    left untouched — this duplicates rather than replaces that
    mapping, so generate()'s already-approved behavior carries zero
    risk from this change.
    """
    if isinstance(exc, AuthenticationError):
        return RuntimeAuthenticationError(str(exc), provider="openai", cause=exc)
    if isinstance(exc, RateLimitError):
        return RuntimeRateLimitError(str(exc), provider="openai", cause=exc)
    if isinstance(exc, APITimeoutError):
        return RuntimeTimeoutError(str(exc), provider="openai", cause=exc)
    return RuntimeProviderError(str(exc), provider="openai", cause=exc)


class _OpenAIStreamIterator:
    """
    Iterator[StreamingEvent] wrapping a raw OpenAI Stream.

    A plain __iter__/__next__ class rather than a generator function:
    Python generators cannot yield after being sent GeneratorExit, so a
    generator could not emit a StreamingCancellation event in response
    to cancel(). This class instead tracks cancellation as explicit
    state, checked at the top of every __next__ call.

    Exactly one terminal event (StreamingCompletion, StreamingCancellation,
    or StreamingError) is returned, after which __next__ raises
    StopIteration on every subsequent call. The underlying OpenAI
    stream is closed exactly once, on whichever terminal path is
    taken.
    """

    def __init__(self, openai_stream: Any) -> None:
        self._openai_stream = openai_stream
        self._cancelled = False
        self._cancel_reason: Optional[StreamCancellationReason] = None
        self._terminated = False
        self._closed = False
        self._last_finish_reason: Optional[str] = None

    def __iter__(self) -> "_OpenAIStreamIterator":
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
                chunk = next(self._openai_stream)
            except StopIteration:
                return self._terminal(
                    StreamingCompletion(finish_reason=self._last_finish_reason)
                )
            except Exception as exc:
                runtime_error = _normalize_openai_exception(exc)
                return self._terminal(
                    StreamingError(message=str(runtime_error), cause=runtime_error)
                )

            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                self._last_finish_reason = choice.finish_reason
            delta = choice.delta
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                return StreamingTextDelta(text=content)
            # Chunk carried no text (e.g. role-only first chunk, or a
            # finish-reason-only final chunk) — fetch the next one.

    def _terminal(self, event: StreamingEvent) -> StreamingEvent:
        self._terminated = True
        self._close()
        return event

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._openai_stream, "close", None)
        if callable(close):
            close()

    def __del__(self) -> None:
        # Defensive safety net: guarantees the underlying stream is
        # released even if the caller drops the iterator without
        # fully consuming or explicitly cancelling it.
        self._close()


class OpenAIProvider(ProviderInterface):
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._model = model
        self._client = OpenAI(api_key=api_key, timeout=timeout)

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        openai_kwargs = self._to_openai_kwargs(request)
        try:
            openai_response = self._client.chat.completions.create(**openai_kwargs)
        except AuthenticationError as exc:
            raise RuntimeAuthenticationError(str(exc), provider="openai", cause=exc) from exc
        except RateLimitError as exc:
            raise RuntimeRateLimitError(str(exc), provider="openai", cause=exc) from exc
        except APITimeoutError as exc:
            raise RuntimeTimeoutError(str(exc), provider="openai", cause=exc) from exc
        except OpenAIError as exc:
            raise RuntimeProviderError(str(exc), provider="openai", cause=exc) from exc
        except Exception as exc:
            raise RuntimeProviderError(str(exc), provider="openai", cause=exc) from exc
        return self._to_provider_response(openai_response)

    def _to_openai_kwargs(self, request: ProviderRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        return kwargs

    def _to_provider_response(self, openai_response: Any) -> ProviderResponse:
        choice = openai_response.choices[0]
        usage = getattr(openai_response, "usage", None)
        metadata = None
        if usage is not None:
            metadata = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        return ProviderResponse(
            text=choice.message.content or "",
            finish_reason=choice.finish_reason,
            metadata=metadata,
        )

    def generate_stream(self, request: ProviderRequest) -> Iterator[StreamingEvent]:
        """
        Generate a response incrementally (H2A-3). Overrides
        ProviderInterface's additive default (H2A-1).

        Establishment-time failures (raised by the SDK call itself,
        before any chunk is received) are normalized exactly like
        mid-stream failures: as a single terminal StreamingError event
        from the returned iterator, never as a raised exception — so
        callers only ever need to inspect the event sequence, never
        catch SDK-specific exceptions from this method itself.
        """
        openai_kwargs = self._to_openai_kwargs(request)
        openai_kwargs["stream"] = True
        try:
            openai_stream = self._client.chat.completions.create(**openai_kwargs)
        except Exception as exc:
            runtime_error = _normalize_openai_exception(exc)
            return iter([StreamingError(message=str(runtime_error), cause=runtime_error)])
        return _OpenAIStreamIterator(openai_stream)
