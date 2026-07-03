"""
provider/errors.py
===================
Runtime-standard provider error hierarchy (H1-2-3: Provider Errors).

These exceptions establish the future normalization target for provider
implementations (H1-3 / H1-4), which shall translate vendor-specific
exceptions into these Runtime-standard types. No provider-specific
exception mapping is implemented here.

EXPORTED API:
  RuntimeProviderError            — base exception for provider failures
  RuntimeAuthenticationError      — authentication / API key failures
  RuntimeRateLimitError           — quota / rate limit failures
  RuntimeTimeoutError             — timeout failures
  RuntimeServiceUnavailableError  — temporary provider outages
"""

from typing import Optional


class RuntimeProviderError(Exception):
    def __init__(
        self,
        message: str,
        provider: Optional[str] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.cause = cause


class RuntimeAuthenticationError(RuntimeProviderError):
    pass


class RuntimeRateLimitError(RuntimeProviderError):
    pass


class RuntimeTimeoutError(RuntimeProviderError):
    pass


class RuntimeServiceUnavailableError(RuntimeProviderError):
    pass
