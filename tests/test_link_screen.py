from textual.app import App
from textual.widgets import Markdown, Static

from harness.link_screen import (
    CodeBlock, CodeBlockPickerScreen, LinkedDocumentScreen, LinkPickerScreen, code_blocks,
)
from harness.markdown_links import MarkdownLink


class ReaderApp(App):
    def __init__(self):
        super().__init__()
        self.opened = []

    async def action_follow_link(self, document_id, index):
        self.opened.append((document_id, index, self.screen.links[index].href))


async def test_reader_keeps_duplicate_unicode_headings_and_original_encoded_links(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    href = "https://example.test/a%252Fb?one=%26&two=%2F"
    spacer = "Paragraph for scrolling.\n\n" * 12
    source = f"# Café\n\n# Café-1\n\n{spacer}> ## Café\n\n[Open page]({href})\n\n{spacer}"
    app = ReaderApp()
    async with app.run_test(size=(35, 16)) as pilot:
        reader = LinkedDocumentScreen("Notes", source, 17, "café-2")
        await app.push_screen(reader)
        await pilot.pause()
        markdown = reader.query_one(Markdown)
        first, _, second = list(markdown.query("MarkdownHeader"))
        assert reader.query_one("#document-scroll").region.contains_region(second.region)
        assert first.region.bottom <= 0
        paragraph = next(block for block in markdown.query("MarkdownParagraph")
                         if str(block.content) == "Open page")
        await pilot.click(paragraph, offset=(2, 0))
        assert app.opened == [(17, 0, href)]

        await pilot.press("l")
        picker = app.screen
        assert isinstance(picker, LinkPickerScreen)
        assert picker.query_one("#link-destination", Static).content == href
        await pilot.press("c")
        assert app.clipboard == href
        assert app.screen is picker
        await pilot.press("enter")
        assert app.screen is reader
        assert app.opened == [(17, 0, href), (17, 0, href)]


async def test_reference_link_with_image_label_uses_only_its_enclosing_destination():
    source = "[![Design](do-not-open.png)][notes]\n\n[notes]: nested%252Fnotes.md#intro"
    app = ReaderApp()
    async with app.run_test(size=(48, 12)) as pilot:
        reader = LinkedDocumentScreen("Index", source, 4)
        await app.push_screen(reader)
        assert reader.links == (MarkdownLink("Design", "nested%252Fnotes.md#intro"),)
        paragraph = reader.query_one("MarkdownParagraph")
        await pilot.click(paragraph, offset=(2, 0))
        assert app.opened == [(4, 0, "nested%252Fnotes.md#intro")]
        await pilot.click("#document-back")
        assert app.screen is not reader


async def test_reopened_picker_reveals_and_copies_failed_selection():
    links = (MarkdownLink("First", "one.md"), MarkdownLink("Second", "two%2520parts.md"))
    app = App()
    async with app.run_test(size=(27, 10)) as pilot:
        picker = LinkPickerScreen(links, initial_index=1)
        await app.push_screen(picker)
        await pilot.pause()
        assert picker.query_one("#link-destination", Static).content == links[1].href
        await pilot.click("#link-copy")
        assert app.clipboard == links[1].href


def test_fences_keep_mixed_line_endings_tabs_empty_lines_and_unterminated_final_line():
    source = ("Inline `shell`\r\n\n~~~python details\r\n\tprint(1)  \r\n\n~~~\r\n\r\n"
              "> ```sh\n> echo 'quoted'\n> ```\n\n```\nunterminated final line")
    assert code_blocks(source) == (
        CodeBlock("python", "\tprint(1)  \r\n\n"),
        CodeBlock("sh", "echo 'quoted'\n"),
        CodeBlock("", "unterminated final line"),
    )


async def test_reader_code_picker_previews_then_copies_exact_block_and_escape_cancels():
    blocks = "\n~~~python\nprint(1)\n~~~\n~~~python\r\n\tprint(2)  \r\n~~~\n"
    app = App()
    async with app.run_test(size=(34, 16)) as pilot:
        reader = LinkedDocumentScreen("Notes", blocks, 7)
        await app.push_screen(reader)
        await pilot.press("y")
        picker = app.screen
        assert isinstance(picker, CodeBlockPickerScreen)
        await pilot.press("down")
        assert picker.query_one("#code-content", Static).content == "\tprint(2)  \n"
        await pilot.press("enter")
        assert app.screen is reader
        assert app.clipboard == "\tprint(2)  \r\n"
        await pilot.click("#document-copy-code")
        await pilot.press("escape")
        assert app.screen is reader
        assert app.clipboard == "\tprint(2)  \r\n"


async def test_reader_single_fence_copies_directly_preserving_empty_fence():
    app = App()
    async with app.run_test(size=(34, 16)) as pilot:
        reader = LinkedDocumentScreen("Notes", "~~~txt\n~~~\n", 8)
        await app.push_screen(reader)
        app.copy_to_clipboard("previous contents")
        await pilot.press("y")
        assert app.screen is reader
        assert app.clipboard == ""
