"""Wait for owned async work to settle despite repeated caller cancellation."""

import asyncio


async def await_owned(worker, *, cancel_on_interrupt=True):
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if cancel_on_interrupt and not worker.done() and not worker.cancelling():
            worker.cancel()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()  # Observe late failure; cancellation remains primary.
        raise
