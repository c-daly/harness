# tests/test_errors.py
from harness.errors import (
    AuthFailed,
    ContextOverflow,
    MalformedStreamError,
    NetworkFailed,
    Overloaded,
    ProviderError,
    RateLimited,
)


def test_hierarchy_and_retryability():
    assert issubclass(RateLimited, ProviderError)
    assert RateLimited.retryable and Overloaded.retryable and NetworkFailed.retryable
    assert not (AuthFailed.retryable or ContextOverflow.retryable)
    assert not MalformedStreamError.retryable


def test_errors_carry_message():
    err = RateLimited("429 from provider")
    assert "429" in str(err)
