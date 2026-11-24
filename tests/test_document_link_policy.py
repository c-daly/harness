"""Exercise link activation across rendering modes and workspace boundaries."""

import webbrowser

from harness.provider import FakeProvider, text_turn
from harness.link_screen import LinkedDocumentScreen, LinkPickerScreen
from harness.tui import MathTranscript
from tests.test_document_link_journey import (
    journal, make_session, screen_lines, see, show_files, submit,
)


async def test_copy_code_uses_latest_examples_and_links_keep_their_source(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.org/first"
    source = "First example\r\n\r\n```sh\r\n  run\tme  \r\n```\r\n\r\n```txt\r\nsecond\r\n```\r\n"
    app = make_session(tmp_path / "state", workspace,
                       FakeProvider([text_turn("See " + url), text_turn(source)]))
    async with app.run_test(size=(48, 25)) as pilot:
        await submit(pilot, "List the address.")
        await see(app, pilot, url)
        await submit(pilot, "Show examples.")
        await see(app, pilot, "First example")
        await submit(pilot, "/copy-code")
        await see(app, pilot, "run")
        await pilot.press("enter")
        assert app.clipboard == "  run\tme  \r\n"
        await submit(pilot, "/copy-code")
        await pilot.press("down", "enter")
        assert app.clipboard == "second\r\n"
        await submit(pilot, "/links")
        await see(app, pilot, url)
        await pilot.press("escape")
    app.kernel.session.close()


async def test_raw_markdown_opens_link_and_same_file_fragment_preserves_reader(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.md").write_text("[Open guide](guide.md#start)\n")
    (workspace / "guide.md").write_text(
        "# Start\n\n[Jump to end](./guide.md#end)\n\n"
        + "Paragraph between sections.\n\n" * 28
        + "## End\n\nThe last section is visible.\n"
    )
    provider = show_files("index.md")
    app = make_session(tmp_path / "state", workspace, provider)
    async with app.run_test(size=(48, 25)) as pilot:
        await submit(pilot, "/markdown off")
        await submit(pilot, "Show index")
        await see(app, pilot, "[Open guide](guide.md#start)")
        await submit(pilot, "/links")
        await see(app, pilot, "guide.md#start")
        assert isinstance(app.screen, LinkPickerScreen)
        await pilot.press("enter")
        target = await see(app, pilot, "Jump to end")
        reader = app.screen
        assert isinstance(reader, LinkedDocumentScreen)
        assert reader.source_path == workspace / "guide.md"
        before_jump = len(journal(app))
        await pilot.click(offset=target)
        await see(app, pilot, "The last section is visible.")
        assert app.screen is reader
        assert all(event.type not in {"tool_call_proposed", "model_call_proposed"}
                   for event in journal(app)[before_jump:])
        await pilot.press("escape")
        assert not isinstance(app.screen, LinkedDocumentScreen)
    app.kernel.session.close()
    assert len(provider.calls) == 1


async def test_unsupported_urls_do_not_launch_and_outside_symlink_obeys_workspace_guard(
    tmp_path, monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "other-project.md"
    outside.write_text("A document in another project.")
    (workspace / "related.md").symlink_to(outside)
    provider = show_files()
    app = make_session(tmp_path / "state", workspace, provider)
    launches = []
    monkeypatch.setattr(webbrowser, "open", launches.append)
    monkeypatch.setattr(app, "open_url", launches.append)
    notices = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(str(message)))
    async with app.run_test(size=(48, 20)) as pilot:
        app._write_reply("[Mailbox](mailto:docs@example.org) · [Elsewhere](//example.org/docs) · "
                         "[Related](related.md)", "index.md")
        log = app.query_one(MathTranscript)
        document_id = next(reversed(log.link_documents))
        before = len(journal(app))
        for index in (0, 1):
            notices.clear()
            app.action_follow_link(document_id, index)
            assert notices
            assert "HTTP" in notices[-1]
        assert not any(event.type == "tool_call_proposed" for event in journal(app)[before:])
        app.action_follow_link(document_id, 2)
        await app._link_worker.wait()
        await pilot.pause()
        assert launches == []
        assert not isinstance(app.screen, LinkedDocumentScreen)
        assert outside.read_text() not in "\n".join(screen_lines(app))
        assert any("outside" in notice for notice in notices)
        events = journal(app)[before:]
        [read] = [event for event in events if event.type == "tool_call_proposed"]
        assert read.tool == "read_file"
        call = [event for event in events if getattr(event, "call_id", None) == read.call_id]
        assert any(event.type == "hook_decided" and event.hook == "workspace-guard"
                   and event.decision["kind"] == "block" for event in call)
        assert all(event.type != "dispatch_resolved" for event in call)
        assert not provider.calls
    app.kernel.session.close()


async def test_partial_scrollback_keeps_source_and_last_line_eviction_releases_it(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = make_session(tmp_path / "state", workspace, show_files())
    async with app.run_test(size=(55, 20)) as pilot:
        await pilot.pause()
        await submit(pilot, "/markdown off")
        log = app.query_one(MathTranscript)
        log.clear()
        log.max_lines = 4
        app._write_reply("Earlier text\n[First](one.md)\nFirst ending")
        app._write_reply("[Second](two.md)")
        first, second = log.link_documents
        log.write("new line")
        assert first in log.link_documents
        log.write("another line")
        log.write("third new line")
        assert list(log.link_documents) == [second]
        log.write("fourth new line")
        assert not log.link_documents
    app.kernel.session.close()
