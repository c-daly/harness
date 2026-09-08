"""Real local inference via CLI startup and the Textual compositor."""
import asyncio
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path

from textual.widgets import Input

import harness.cli
import harness.tui
from harness.errors import ContextOverflow
from harness.events import UserMessage
from harness.fold import fold
from harness.inference import InferenceRequest, input_bytes
from harness.log import read_session
from harness.messages import Message

OUT = Path(__file__).parent
REPORT = {}
USER_CATALOG = Path.home() / '.config/harness/models.toml'
BEFORE = hashlib.sha256(USER_CATALOG.read_bytes()).hexdigest()


def listening():
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(('127.0.0.1', 8186)) == 0


def visible(app):
    return '\n'.join(strip.text for strip in app.screen._compositor.render_strips())


def seed(kernel, text):
    kernel.session.append(UserMessage(text=text))
    kernel.loop.history.append(Message.user_text(text))


async def launch(kernel, **kwargs):
    app = harness.tui.HarnessApp(kernel, **kwargs)
    try:
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause(0.1)
            seed(kernel, 'Project codename is amberfern. Preserve this exact codename. '
                 'The numeric diagnostic samples in the next message are disposable filler, not project facts.')
            seed(kernel, '\n'.join(f'x{n}={n};' for n in range(2400)))
            seed(kernel, 'Release token is skyglass. Next task is to test /project/next.py. '
                 'That test is still pending. Preserve this token and unresolved task.')
            REPORT['source_bytes'] = input_bytes(kernel.loop.history, (), limit=4 * 1024 * 1024)
            original = list(kernel.loop.history)
            started = time.monotonic()
            try:
                await kernel.loop.dispatcher.dispatch_inference(
                    provider=kernel.provider,
                    request=InferenceRequest(model=kernel.loop.model, purpose='compaction-legacy-probe',
                        messages=(Message.system_text(kernel.loop.system_prompt), *kernel.loop.history,
                                  Message.user_text('Summarize this conversation.')),
                        max_output_tokens=128), pinned=True, exact_model=True,
                )
            except ContextOverflow as exc:
                REPORT['whole_history_failure'] = str(exc)[:300]
            else:
                raise AssertionError('whole-history probe unexpectedly fit; this does not demonstrate recovery')
            REPORT['whole_history_seconds'] = round(time.monotonic() - started, 3)
            assert kernel.loop.history == original
            prompt = app.query_one('#prompt', Input)
            prompt.value = '/compact'
            start = time.monotonic()
            await pilot.press('enter')
            prompt.value = 'unsent continuation draft'
            progress = []
            async with asyncio.timeout(320):
                while app._compact_worker is not None:
                    text = visible(app)
                    line = next((line.strip() for line in text.splitlines() if 'Compacting ' in line), None)
                    if line and (not progress or line != progress[-1]):
                        progress.append(line)
                        print(line, flush=True)
                        (OUT / 'progress-terminal.svg').write_text(app.export_screenshot())
                    await asyncio.sleep(0.1)
            await pilot.pause(0.2)
            REPORT['compact_seconds'] = round(time.monotonic() - start, 3)
            evs = read_session(kernel.session.base, kernel.session.id)
            compact = [e.event for e in evs if e.event.type == 'compaction_applied']
            if not compact:
                (OUT / 'failure-terminal.svg').write_text(app.export_screenshot())
                raise AssertionError(visible(app))
            assert len(compact) == 1
            REPORT['summary'] = compact[0].summary
            assert 'amberfern' in REPORT['summary'] and 'skyglass' in REPORT['summary']
            assert 'pending' in REPORT['summary'].lower() and '/project/next.py' in REPORT['summary']
            assert len(kernel.loop.history) == 1 and kernel.loop.history == fold(evs).messages
            REPORT['parts'] = len([e for e in evs if e.event.type == 'model_call_completed' and e.event.purpose == 'compaction'])
            assert REPORT['parts'] > 1 and len(progress) == REPORT['parts']
            assert prompt.value == 'unsent continuation draft'
            REPORT['progress'] = progress
            REPORT['draft_preserved'] = True
            start = time.monotonic()
            prompt.value = ('What are the project codename and release token, and which task is still pending? '
                            'Answer briefly without using tools.')
            await pilot.press('enter')
            prompt.value = 'unsent continuation draft'
            async with asyncio.timeout(180):
                while app.controller.active is not None or (app._turn_worker is not None and not app._turn_worker.is_finished):
                    await asyncio.sleep(0.1)
            await pilot.pause(0.2)
            REPORT['continuation_seconds'] = round(time.monotonic() - start, 3)
            evs = read_session(kernel.session.base, kernel.session.id)
            call = [e.event for e in evs if e.event.type == 'model_call_completed' and e.event.purpose == 'conversation'][-1]
            answer = Message.model_validate(call.message).text()
            assert 'amberfern' in answer and 'skyglass' in answer and '/project/next.py' in answer
            assert app.controller.last_result.status == 'completed'
            assert 'amberfern' in visible(app) and 'skyglass' in visible(app)
            assert prompt.value == 'unsent continuation draft'
            (OUT / 'complete-terminal.svg').write_text(app.export_screenshot())
            REPORT.update(answer=answer, session_id=str(kernel.session.id), model=str(kernel.loop.model),
                          compositor_verified=True)
    finally:
        await app._finish()
        app.kernel.session.close()
        REPORT['user_catalog_unchanged'] = BEFORE == hashlib.sha256(USER_CATALOG.read_bytes()).hexdigest()
        REPORT['owned_runtime_stopped'] = not listening()
        (OUT / 'report.json').write_text(json.dumps(REPORT, indent=2) + '\n')
    assert REPORT['user_catalog_unchanged'] and REPORT['owned_runtime_stopped']


assert not listening(), 'test port already in use'
harness.tui.run_tui = launch
os.chdir(OUT)
sys.argv = ['harness', '--base-dir', str(OUT / 'sessions'), '--catalog', str(OUT / 'models.toml'),
            '--workspace', str(OUT), '--no-mcp', '--no-plugins', '--model', 'compaction-gpu']
harness.cli.main()
print(json.dumps(REPORT, indent=2))
