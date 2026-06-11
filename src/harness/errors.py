# src/harness/errors.py
"""Typed provider failures. Adapters raise these; the kernel decides retry."""

from typing import ClassVar


class ProviderError(Exception):
    retryable: ClassVar[bool] = False


class RateLimited(ProviderError):
    retryable: ClassVar[bool] = True


class Overloaded(ProviderError):
    retryable: ClassVar[bool] = True


class NetworkFailed(ProviderError):
    retryable: ClassVar[bool] = True


class ContextOverflow(ProviderError):
    retryable: ClassVar[bool] = False


class AuthFailed(ProviderError):
    retryable: ClassVar[bool] = False


class MalformedStreamError(ProviderError):
    """The provider stream violated the chunk contract (unparseable tool args,
    missing id/name). Not retryable: the same request likely fails the same way."""

    retryable: ClassVar[bool] = False
