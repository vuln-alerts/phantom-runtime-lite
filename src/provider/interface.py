"""
provider/interface.py
======================
Abstract Provider Interface (H1-2-4: Provider Interface;
H2A-1: Streaming Interface Extension).

Defines the Runtime's provider abstraction boundary. Provider
implementations (H1-3 / H1-4) shall inherit from ProviderInterface and
implement generate(). This interface contains no SDK logic, routing,
retry logic, authentication, network communication, or provider
selection — those belong to provider implementations and later phases.

The Runtime Core continues to bypass this interface during H1-2/H1-3;
it is not yet used by any execution path.

--------------------------------------------------------------------
Streaming Extension (H2A-1)
--------------------------------------------------------------------
Per V1_11_PROVIDER_INTERFACE_STREAMING_AMENDMENT_CONTRACT.md, Option A
("Separate Streaming API") was adopted: streaming is introduced as a
second, additive operation — generate_stream() — alongside the
existing buffered generate(). generate() is unchanged; no existing
buffered implementation requires modification because of this
extension.

generate_stream() is deliberately NOT an abstractmethod. Its default
implementation raises NotImplementedError, so any existing or future
ProviderInterface subclass that implements only generate() remains
fully valid, instantiable, and behaviorally unchanged — satisfying
Backward Compatibility. Providers that support streaming (H2A-3
onward) override generate_stream() explicitly.

This phase (H2A-1) defines the interface operation only. The concrete
per-increment stream item type, completion signal, cancellation
mechanism, and disposal/lifecycle contract are intentionally left
unspecified here — they belong to H2A-2 (Streaming Models &
Lifecycle), which is out of scope for this phase. The Iterator[Any]
return annotation below is a placeholder pending that phase; no new
provider-independent streaming model is introduced by H2A-1.

EXPORTED API:
  ProviderInterface — abstract base class for provider implementations
"""

from abc import ABC, abstractmethod
from typing import Any, Iterator

from provider.models import ProviderRequest, ProviderResponse


class ProviderInterface(ABC):
    @abstractmethod
    def generate(self, request: ProviderRequest) -> ProviderResponse:
        """
        Generate a response for the given provider-independent request.

        Raises RuntimeProviderError (see provider.errors) or a subclass
        thereof when the underlying provider implementation fails.
        """
        raise NotImplementedError

    def generate_stream(self, request: ProviderRequest) -> Iterator[Any]:
        """
        Generate a response incrementally for the given
        provider-independent request (H2A-1: additive streaming
        extension; see module docstring).

        Not an abstractmethod: providers that do not implement
        streaming inherit this default, which raises
        NotImplementedError. This preserves Backward Compatibility for
        every existing ProviderInterface subclass.

        The concrete increment type, completion signal, and
        cancellation/disposal lifecycle are defined in H2A-2 and are
        not yet specified by this phase.
        """
        raise NotImplementedError
