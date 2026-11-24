"""Link destinations and visible labels from parsed Markdown documents."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline, linkify
from markdown_it.token import Token


@dataclass(frozen=True)
class MarkdownLink:
    label: str
    href: str


def _bare_web_link(state: StateInline, silent: bool) -> bool:
    """Let MarkdownIt bound the URL and keep the source spelling of its label."""
    if state.src[state.pos] != ":":
        return False
    prefix = state.src[max(0, state.pos - 5):state.pos].lower()
    if prefix.endswith("https"):
        start = state.pos - 5
    elif prefix.endswith("http"):
        start = state.pos - 4
    else:
        return False
    if not linkify(state, silent):
        return False
    if not silent:
        # The native rule emitted open/text/close and URL-normalized the label.
        # Keep pasted escapes legible, even when their decoded form is a space.
        state.tokens[-2].content = state.src[start:state.pos]
    return True


class DocumentMarkdownIt(MarkdownIt):
    """The Markdown dialect shared by the transcript, reader, and plain-text picker."""

    def __init__(self) -> None:
        super().__init__(options_update={"linkify": True})
        self.enable(["strikethrough", "table"])
        self.inline.ruler.at("linkify", _bare_web_link)
        self.inline.ruler.enable("linkify")


def parse_markdown_links(source: str) -> tuple[MarkdownLink, ...]:
    return collect_markdown_links(DocumentMarkdownIt().parse(source))


def is_web_link(href: str) -> bool:
    """Return whether the target can be handed directly to a terminal or browser."""
    if "\\" in href or any(c.isspace() or not c.isprintable() for c in href):
        return False
    try:
        url = urlsplit(href)
        if url.scheme not in {"http", "https"} or not url.hostname:
            return False
        # Accessing .port also validates its syntax and range.
        _ = url.port
    except ValueError:
        return False
    return True


def workspace_link(href: str, workspace: Path, source: Path | None) -> tuple[Path | None, str]:
    """Split before decoding so encoded '#' characters remain part of a filename."""
    url = urlsplit(href)
    path = unquote(url.path, errors="strict")
    fragment = unquote(url.fragment, errors="strict")
    if (url.scheme or url.netloc or url.query or path.startswith("//") or "\\" in path
            or not all(c.isprintable() for c in path + fragment)):
        raise ValueError("Harness can open workspace files, document sections, and HTTP or HTTPS URLs.")
    base = source.parent if source else workspace
    return ((base / path).resolve() if path else None), fragment


def _inline_tokens(tokens: Iterable[Token]) -> Iterator[Token]:
    for token in tokens:
        if token.children and token.type != "image":
            yield from _inline_tokens(token.children)
        else:
            yield token


def _label_text(token: Token) -> str:
    if token.type in {"text", "code_inline"}:
        return token.content
    if token.type in {"softbreak", "hardbreak"}:
        return " "
    if token.type == "image":
        return "".join(_label_text(child) for child in token.children or ())
    return ""


def collect_markdown_links(tokens: Iterable[Token]) -> tuple[MarkdownLink, ...]:
    """Index actual link tokens in source order, including resolved reference links."""
    links: list[MarkdownLink] = []
    opener = None
    label: list[str] = []
    for token in _inline_tokens(tokens):
        if token.type == "link_open":
            opener = token
            opener.meta["harness_link_index"] = len(links)
            label.clear()
        elif opener is not None:
            if token.type == "link_close":
                links.append(MarkdownLink("".join(label), str(opener.attrGet("href") or "")))
                opener = None
            else:
                label.append(_label_text(token))
    return tuple(links)
