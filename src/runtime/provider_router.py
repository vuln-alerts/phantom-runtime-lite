"""
runtime/provider_router.py
=============================
Provider Router (H5-1: Request-Based Multi-Provider Runtime).

Validates and selects the provider identifier for a single Runtime
session. This module owns exactly three responsibilities — provider
validation, provider selection, and exposing the selected value for
dispatch — and nothing else:

  - no SDK calls (no openai / google-genai imports)
  - no HTTP/WebSocket framework dependency
  - no provider-specific business logic

Provider implementations (provider.openai_provider.OpenAIProvider,
provider.gemini_provider.GeminiProvider) and the Runtime child
(phantom_runtime.py) that constructs them remain the only places that
know how to actually talk to a provider. This module only decides
*which* one a session gets, given client-supplied request metadata —
today, the `provider` query parameter on the WebSocket handshake
request (see runtime.transport_gateway), reflecting the per-connection
session-scoped routing model.

EXPORTED API:
  SUPPORTED_PROVIDERS        -- frozenset of valid provider identifiers
  ProviderRejected           -- raised for missing/unknown provider
  select_provider_from_query -- validate + normalize a query string
"""

from urllib.parse import parse_qs

SUPPORTED_PROVIDERS = frozenset({"openai", "gemini"})


class ProviderRejected(Exception):
    """Raised when a session's requested provider is missing or unrecognized.

    Callers (e.g. runtime.transport_gateway) are expected to map this to
    an HTTP 400 Bad Request response, using str(exc) as the body.
    """


def select_provider_from_query(query_string: str) -> str:
    """
    Extract and validate the `provider` parameter from a WebSocket
    handshake request's query string (e.g. "provider=gemini").

    Raises ProviderRejected("missing provider") if the parameter is
    absent or empty, or ProviderRejected("unknown provider: <value>")
    if it is not one of SUPPORTED_PROVIDERS. Returns the lowercased,
    validated provider identifier otherwise.
    """
    values = parse_qs(query_string or "").get("provider")
    if not values or not values[0].strip():
        raise ProviderRejected("missing provider")

    provider = values[0].strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ProviderRejected(f"unknown provider: {provider}")

    return provider
