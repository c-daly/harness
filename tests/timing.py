"""Control real inference deadlines after the stream reaches the intended phase."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace


@contextmanager
def inference_timers(monkeypatch):
    import harness.inference

    active = []

    @asynccontextmanager
    async def tracked(seconds):
        timer = asyncio.timeout(seconds)
        async with timer:
            item = (seconds, timer)
            active.append(item)
            try:
                yield timer
            finally:
                active.remove(item)

    # Instrument only the inference module, leaving task/coordinator timers and
    # the test's own bounded waits untouched.
    with monkeypatch.context() as patch:
        patch.setattr(harness.inference, "asyncio", SimpleNamespace(**{
            **vars(asyncio), "timeout": tracked}))
        yield active
