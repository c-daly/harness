"""Keyboard and mouse navigation for links in a terminal session."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from markdown_it.token import Token
from markdown_it.utils import EnvType
from pygments.lexers import get_lexer_for_filename
from pygments.util import ClassNotFound
from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual._slug import slug
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ItemGrid, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from harness.cli import terminal_text
from harness.markdown_links import DocumentMarkdownIt, MarkdownLink, collect_markdown_links


def document_source(source: str) -> str:
    """Sanitize terminal text while preserving paired CRLF for copying."""
    return "\r\n".join(terminal_text(part) for part in source.split("\r\n"))


def source_text(path: str, source: str, *, highlight: bool) -> Text | Syntax:
    text = terminal_text(source)
    if highlight:
        try:
            lexer = get_lexer_for_filename(path)
        except ClassNotFound:
            pass
        else:
            return Syntax(text, lexer, word_wrap=True, background_color="default")
    return Text(text)


@dataclass(frozen=True)
class CodeBlock:
    language: str
    text: str


def code_blocks(source: str, *, tokens: Sequence[Token] | None = None) -> tuple[CodeBlock, ...]:
    """Extract fenced code without the delimiters or enclosing list/quote markup."""
    source = document_source(source)
    if tokens is None:
        tokens = DocumentMarkdownIt().parse(source)
    endings = re.findall(r"\r\n|\n", source)
    blocks = []
    for token in tokens:
        if token.type != "fence" or token.map is None:
            continue
        # MarkdownIt removes the container prefix and normalizes line endings.
        # Put back the source terminator for each actual line of code.
        lines = token.content.split("\n")
        first_line = token.map[0] + 1
        text = "".join(line + (endings[i] if i < len(endings) else "")
                       for i, line in enumerate(lines[:-1], first_line)) + lines[-1]
        language = token.info.strip().split(maxsplit=1)
        blocks.append(CodeBlock(language[0] if language else "", text))
    return tuple(blocks)


class CodeBlockPickerScreen(ModalScreen[str | None]):
    """Preview and choose a fence by its language and code."""

    AUTO_FOCUS = "#code-options"
    BINDINGS = [Binding("escape", "back", "Back")]
    DEFAULT_CSS = """
    CodeBlockPickerScreen { align: center middle; }
    CodeBlockPickerScreen #code-box {
        width: 100%; max-width: 76; height: 90%; max-height: 24;
        border: round $accent; background: $surface; padding: 0 1;
    }
    CodeBlockPickerScreen #code-options { height: 1fr; border: none; }
    CodeBlockPickerScreen #code-preview { height: 1fr; border-top: solid $panel; }
    CodeBlockPickerScreen #code-actions { height: auto; }
    CodeBlockPickerScreen Button { width: auto; min-width: 0; margin-right: 1; }
    """

    def __init__(self, blocks: Sequence[CodeBlock]):
        super().__init__()
        self.blocks = tuple(blocks)

    def compose(self) -> ComposeResult:
        with Vertical(id="code-box"):
            yield Static("Copy code block")
            options = []
            for i, block in enumerate(self.blocks, 1):
                first_line = block.text.lstrip().partition("\n")[0].rstrip()
                language = block.language or "Code"
                options.append(Option(Text(f"{i}. {language} · {first_line or '(empty)'}")))
            yield OptionList(*options, id="code-options")
            with VerticalScroll(id="code-preview"):
                yield Static("", markup=False, id="code-content")
            with Horizontal(id="code-actions"):
                yield Button("Copy (Enter)", id="code-copy", compact=True)
                yield Button("Back (Esc)", id="code-back", compact=True)

    @on(OptionList.OptionHighlighted)
    def _preview(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self.query_one("#code-content", Static).update(terminal_text(self.blocks[event.option_index].text))
        self.query_one("#code-preview", VerticalScroll).scroll_home(animate=False)

    @on(OptionList.OptionSelected)
    def _select(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if self.is_current:
            self.dismiss(self.blocks[event.option_index].text)

    @on(Button.Pressed, "#code-copy")
    def _copy(self) -> None:
        self.query_one("#code-options", OptionList).action_select()

    @on(Button.Pressed, "#code-back")
    def action_back(self) -> None:
        self.dismiss(None)


def copy_code_blocks(app: App, blocks: Sequence[CodeBlock]) -> str | None:
    """Copy one fence immediately, or copy the chosen fence when the picker closes."""
    def copy(code: str | None) -> str | None:
        if code is not None:
            app.copy_to_clipboard(code)
            app.notify("Code block sent to the terminal clipboard.")
        return code

    if len(blocks) == 1:
        return copy(blocks[0].text)
    if blocks:
        app.push_screen(CodeBlockPickerScreen(blocks), copy)
    else:
        app.notify("No fenced code blocks in this document.")
    return None


class LinkPickerScreen(ModalScreen[int | None]):
    """Reveal and copy original destinations, returning the chosen index on open."""

    AUTO_FOCUS = "#link-options"
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("c", "copy_destination", "Copy destination"),
    ]
    DEFAULT_CSS = """
    LinkPickerScreen { align: center middle; }
    LinkPickerScreen #link-box {
        width: 100%; max-width: 76; height: 90%; max-height: 24;
        border: round $accent; background: $surface; padding: 0 1;
    }
    LinkPickerScreen #link-options { height: 1fr; border: none; }
    LinkPickerScreen #link-detail { height: auto; max-height: 35%; border-top: solid $panel; }
    LinkPickerScreen #link-actions { height: auto; }
    LinkPickerScreen Button { width: auto; min-width: 0; margin-right: 1; }
    """

    def __init__(self, links: Sequence[MarkdownLink], *, initial_index: int = 0):
        super().__init__()
        self.links = tuple(links)
        self._initial_index = initial_index if 0 <= initial_index < len(self.links) else 0

    def compose(self) -> ComposeResult:
        with Vertical(id="link-box"):
            yield Static("Links")
            yield OptionList(
                *(Option(Text(link.label or link.href)) for link in self.links),
                id="link-options",
            )
            with VerticalScroll(id="link-detail"):
                destination = (self.links[self._initial_index].href if self.links
                               else "No links in this document.")
                yield Static(destination, markup=False, id="link-destination")
            with Horizontal(id="link-actions"):
                yield Button("Open", id="link-open", compact=True, disabled=not self.links)
                yield Button("Copy (c)", id="link-copy", compact=True, disabled=not self.links)
                yield Button("Back (Esc)", id="link-back", compact=True)

    def on_mount(self) -> None:
        if self.links:
            self.query_one("#link-options", OptionList).highlighted = self._initial_index

    @on(OptionList.OptionHighlighted)
    def _reveal(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self.query_one("#link-destination", Static).update(self.links[event.option_index].href)
        self.query_one("#link-detail", VerticalScroll).scroll_home(animate=False)

    @on(OptionList.OptionSelected)
    def _open_option(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if self.is_current:
            self.dismiss(event.option_index)

    @on(Button.Pressed, "#link-open")
    def _open_button(self) -> None:
        self.query_one("#link-options", OptionList).action_select()

    @on(Button.Pressed, "#link-copy")
    def action_copy_destination(self) -> None:
        index = self.query_one("#link-options", OptionList).highlighted
        if index is not None:
            self.app.copy_to_clipboard(self.links[index].href)
            self.notify("Link sent to the terminal clipboard.")

    @on(Button.Pressed, "#link-back")
    def action_back(self) -> None:
        self.dismiss(None)


class _DocumentParser(DocumentMarkdownIt):
    """Keep the displayed tokens and keyboard destinations in the same order."""

    def __init__(self, source: str):
        super().__init__()
        self.source = source
        self.tokens = super().parse(source)
        self.links = collect_markdown_links(self.tokens)
        for block in self.tokens:
            for child in block.children or ():
                if child.type == "link_open":
                    # Textual URL-decodes LinkClicked.href. Only an index goes through
                    # that conversion, so encoded filenames and web URLs survive intact.
                    child.attrSet("href", str(child.meta["harness_link_index"]))
                elif child.type == "image":
                    # Keep the alt text (and enclosing link, when present). Native
                    # Markdown would also turn the image's src into a clickable action.
                    child.type = "text"
                    child.content = child.content or "Image"

    def parse(self, src: str, env: EnvType | None = None) -> list[Token]:
        if src == self.source:
            return self.tokens
        return super().parse(src, env)


class LinkedDocumentScreen(ModalScreen[None]):
    """Read a checked source snapshot, keeping the previous screen on the stack."""

    AUTO_FOCUS = "#document-scroll"
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("l", "links", "Links"),
        Binding("y", "copy_code", "Copy code"),
    ]
    DEFAULT_CSS = """
    LinkedDocumentScreen { background: $surface; }
    LinkedDocumentScreen #document-title { text-style: bold; padding: 0 1; max-height: 2; }
    LinkedDocumentScreen #document-scroll { height: 1fr; }
    LinkedDocumentScreen #document-text, LinkedDocumentScreen Markdown { padding: 0 1; }
    LinkedDocumentScreen MarkdownBlock, LinkedDocumentScreen MarkdownTableCellContents {
        link-style: underline;
    }
    LinkedDocumentScreen #document-actions { height: auto; padding: 0 1; }
    LinkedDocumentScreen Button { width: auto; min-width: 0; margin-right: 1; }
    """

    def __init__(self, title: str, source: str, document_id: int, fragment: str = "",
                 *, markdown: bool = True, source_path: Path | None = None, highlight: bool = True):
        super().__init__()
        self.document_title = terminal_text(title)
        self.document_id = document_id
        self.source_path = source_path
        self.source = document_source(source)
        self._fragment = fragment
        self._highlight = highlight
        self._parser = _DocumentParser(terminal_text(self.source)) if markdown else None
        self.links = self._parser.links if self._parser else ()
        self.blocks = code_blocks(self.source, tokens=self._parser.tokens) if self._parser else ()

    def compose(self) -> ComposeResult:
        yield Static(self.document_title, id="document-title", markup=False)
        with VerticalScroll(id="document-scroll"):
            if self._parser is not None:
                parser = self._parser
                yield Markdown(parser.source, id="document-markdown", open_links=False,
                               parser_factory=lambda: parser)
            else:
                yield Static(source_text(self.document_title, self.source, highlight=self._highlight),
                             id="document-text", markup=False)
        with ItemGrid(id="document-actions", min_column_width=15, max_column_width=18,
                      stretch_height=False):
            yield Button("Back (Esc)", id="document-back", compact=True)
            yield Button("Links (l)", id="document-links", compact=True, disabled=not self.links)
            if self.blocks:
                yield Button("Copy code (y)", id="document-copy-code", compact=True)

    def on_mount(self) -> None:
        if self._fragment and self._parser is None:
            self.call_after_refresh(self.jump_to_heading, self._fragment)

    @on(Markdown.TableOfContentsUpdated)
    def _document_ready(self) -> None:
        # Textual mounts large documents in batches. Wait for the last batch
        # before looking up a heading that may be near the end of the source.
        if self._fragment:
            self.call_after_refresh(self.jump_to_heading, self._fragment)
            self._fragment = ""

    def jump_to_heading(self, fragment: str) -> bool:
        if not fragment:
            self.query_one("#document-scroll", VerticalScroll).scroll_home(animate=False)
            return True
        if self._parser:
            # The default Markdown TOC only includes direct children. Include
            # headings inside quotes and lists in the same duplicate numbering.
            target = quote(fragment)
            used: set[str] = set()
            suffixes: dict[str, int] = {}
            for header in self.query_one(Markdown).query("MarkdownHeader").results(Static):
                base = slug(str(header.content))
                anchor = base
                while anchor in used:
                    suffixes[base] = suffixes.get(base, 0) + 1
                    anchor = f"{base}-{suffixes[base]}"
                if anchor == target:
                    header.scroll_visible(animate=False, top=True)
                    return True
                used.add(anchor)
        self.notify(f"Section #{terminal_text(fragment)} could not be found.", severity="warning", markup=False)
        return False

    @on(Markdown.LinkClicked)
    async def _clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        if event.href.isascii() and event.href.isdecimal():
            await self._follow_index(int(event.href))

    async def _follow_index(self, index: int | None) -> None:
        if index is not None and 0 <= index < len(self.links):
            await self.app.run_action(("app", "follow_link", (self.document_id, index)))

    @on(Button.Pressed, "#document-links")
    def action_links(self) -> None:
        if self.links:
            self.app.push_screen(LinkPickerScreen(self.links), self._follow_index)

    @on(Button.Pressed, "#document-copy-code")
    def action_copy_code(self) -> None:
        copy_code_blocks(self.app, self.blocks)

    @on(Button.Pressed, "#document-back")
    def action_back(self) -> None:
        self.dismiss()
