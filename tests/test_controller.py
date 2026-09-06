import asyncio

import pytest

from harness.controller import InteractionController
from harness.agent import AgentResult


async def test_queue_acceptance_is_ordered_and_does_not_start_another_turn():
    controller = InteractionController(max_pending=2)
    first = controller.submit("first")
    entered, release = asyncio.Event(), asyncio.Event()
    ran = []

    async def execute(prompt):
        ran.append(prompt.text)
        entered.set()
        await release.wait()

    task = asyncio.create_task(controller.run_next(execute))
    await entered.wait()
    second = controller.submit("second")
    assert controller.active == first
    assert controller.pending == (second,)
    assert await controller.run_next(execute) is False
    release.set()
    await task
    assert ran == ["first"]
    await controller.run_next(execute)
    assert ran == ["first", "second"]


async def test_failure_pauses_queue_without_replaying_started_work():
    controller = InteractionController()
    failed = controller.submit("side effects")
    pending = controller.submit("follow up")

    async def fail(prompt):
        raise RuntimeError("interrupted after a write")

    with pytest.raises(RuntimeError):
        await controller.run_next(fail)
    assert controller.paused
    assert controller.last_failed == failed
    assert controller.pending == (pending,)
    assert await controller.run_next(fail) is False
    controller.resume()
    calls = []

    async def succeed(prompt):
        calls.append(prompt.text)

    await controller.run_next(succeed)
    assert calls == ["follow up"]


async def test_cancel_pauses_queue_and_preserves_active_prompt_for_inspection():
    controller = InteractionController()
    first = controller.submit("active")
    second = controller.submit("queued")
    entered = asyncio.Event()

    async def wait(prompt):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(controller.run_next(wait))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller.phase == "interrupted"
    assert controller.paused and controller.active is None
    assert controller.last_failed == first
    assert controller.pending == (second,)


async def test_incomplete_result_pauses_followups_until_explicit_resume():
    controller = InteractionController()
    active = controller.submit("work")
    followup = controller.submit("use the result")
    result = AgentResult(task_id="task", run_id="run", status="incomplete", reason="max_tokens")

    async def execute(prompt):
        return result

    assert await controller.run_next(execute)
    assert controller.phase == "incomplete" and controller.paused
    assert controller.last_result == result and controller.last_failed == active
    assert controller.pending == (followup,)
    assert not await controller.run_next(execute)


def test_queue_capacity_and_edit_remove_are_explicit():
    controller = InteractionController(max_pending=1, max_prompt_chars=10)
    prompt = controller.submit("draft")
    with pytest.raises(ValueError, match="full"):
        controller.submit("next")
    with pytest.raises(ValueError, match="characters"):
        controller.edit(prompt.id, "too many characters")
    controller.edit(prompt.id, "revised")
    assert controller.remove(prompt.id).text == "revised"
    assert controller.pending == ()
    with pytest.raises(KeyError):
        controller.remove(prompt.id)
