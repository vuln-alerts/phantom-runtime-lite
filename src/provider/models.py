"""
provider/models.py
===================
Provider-independent request/response models (H1-2-2: Provider Models).
Streaming models & lifecycle (H2A-2: Streaming Models & Lifecycle).

Provider implementations translate these models into provider-specific
request/response formats. No provider-specific fields are permitted here.

--------------------------------------------------------------------
Streaming Models & Lifecycle (H2A-2)
--------------------------------------------------------------------
Per V1_11_PROVIDER_INTERFACE_STREAMING_AMENDMENT_CONTRACT.md, these
models are purely additive and provider-independent. They define the
data shapes a streaming-capable Provider Implementation will emit
(H2A-3) and that ProviderInterface.generate_stream() will be typed
against once implemented (H2A-3+) — no streaming behavior, no
generate_stream() implementation, and no SDK/HTTP-specific structure
is introduced here.

A streaming generation is modeled as a sequence of StreamingEvent
values (StreamingTextDelta | StreamingCompletion | StreamingCancellation
| StreamingError), together with a StreamLifecycleState describing
which phase of that sequence a given moment belongs to:

    STARTED -> STREAMING -> {COMPLETED, CANCELLED, ERRORED}

STARTED and STREAMING are non-terminal. COMPLETED, CANCELLED, and
ERRORED are terminal — reaching any one of them implies the Provider
Implementation has released its underlying resources (Contract §4.4);
no further StreamingEvent may be emitted afterward. This module
defines TERMINAL_STREAM_LIFECYCLE_STATES as the authoritative set of
terminal states; it is a plain data constant, not lifecycle-transition
behavior.

StreamingError.cause is typed as Optional[Exception] rather than
Optional[RuntimeProviderError] — matching the same choice already made
in provider.errors.RuntimeProviderError.cause — so that this module
introduces no dependency on provider.errors and remains maximally
provider-independent. It must never hold a provider SDK exception type
(e.g. openai.OpenAIError); providers populate it only after any
Runtime-standard error normalization has already occurred.

EXPORTED API:
  Message                          — single conversational message (role, content)
  ProviderRequest                  — provider-independent request
  ProviderResponse                 — provider-independent response
  ProviderMetadata                 — optional provider-independent metadata envelope
  StreamLifecycleState             — phase of a streaming generation
  TERMINAL_STREAM_LIFECYCLE_STATES — terminal StreamLifecycleState values
  StreamCancellationReason         — why a stream was cancelled
  StreamingMetadata                — optional provider-independent stream metadata
  StreamingTextDelta                — one incremental text fragment
  StreamingCompletion               — terminal completion signal (finish_reason + metadata)
  StreamingCancellation             — terminal cancellation signal
  StreamingError                    — terminal error signal (message + optional cause)
  StreamingEvent                    — union of the above four event types
"""

import enum
from dataclasses import dataclass
from typing import Any, Optional, Union


@dataclass
class Message:
    role: str
    content: str


@dataclass
class ProviderMetadata:
    values: Optional[dict[str, Any]] = None


@dataclass
class ProviderRequest:
    messages: list[Message]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    metadata: Optional[dict[str, Any]] = None


@dataclass
class ProviderResponse:
    text: str
    finish_reason: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class StreamLifecycleState(enum.Enum):
    STARTED   = "started"
    STREAMING = "streaming"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERRORED   = "errored"


TERMINAL_STREAM_LIFECYCLE_STATES = frozenset({
    StreamLifecycleState.COMPLETED,
    StreamLifecycleState.CANCELLED,
    StreamLifecycleState.ERRORED,
})


class StreamCancellationReason(enum.Enum):
    CALLER_REQUESTED  = "caller_requested"
    DEADLINE_EXCEEDED = "deadline_exceeded"


@dataclass(frozen=True)
class StreamingMetadata:
    values: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class StreamingTextDelta:
    text: str


@dataclass(frozen=True)
class StreamingCompletion:
    finish_reason: Optional[str] = None
    metadata: Optional[StreamingMetadata] = None


@dataclass(frozen=True)
class StreamingCancellation:
    reason: Optional[StreamCancellationReason] = None


@dataclass(frozen=True)
class StreamingError:
    message: str
    cause: Optional[Exception] = None


StreamingEvent = Union[
    StreamingTextDelta,
    StreamingCompletion,
    StreamingCancellation,
    StreamingError,
]
