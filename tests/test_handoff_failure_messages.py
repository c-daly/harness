"""Report whether a failed operator command actually consumed a handoff record."""

import pytest

from harness.handoff_cli import HandoffAttemptError, failure_message, perform
from tests.test_handoff import source, specification


async def test_started_provider_failure_explains_reconciliation_without_private_error_body(tmp_path):
    kernel, provider = await source(tmp_path)
    provider.steps = ["fail"]
    record = kernel.handoffs.record(specification(kernel))
    try:
        with pytest.raises(HandoffAttemptError) as error:
            await perform(kernel, ["run", record.id])
        message = failure_message(error.value)
        assert "Handoff failed: AuthFailed" in message
        assert "expired after continuation side effect" not in message
        assert "record a new handoff before another attempt" in message
        assert kernel.tasks.selected().execution == "failed"
        with pytest.raises(ValueError, match="already attempted") as refused:
            await perform(kernel, ["run", record.id])
        assert not isinstance(refused.value, HandoffAttemptError)
        assert failure_message(refused.value).startswith("Handoff refused:")
    finally:
        kernel.session.close()


async def test_incomplete_return_also_explains_that_the_record_was_used(tmp_path):
    kernel, provider = await source(tmp_path)
    # Repeated disallowed writes exhaust the bounded iteration count without success.
    provider.steps = ["old"] * 8
    record = kernel.handoffs.record(specification(kernel))
    try:
        message = await perform(kernel, ["run", record.id])
        assert message.startswith("Handoff incomplete:")
        assert "record a new handoff before another attempt" in message
        assert not (provider.root / "B.txt").exists()
    finally:
        kernel.session.close()
