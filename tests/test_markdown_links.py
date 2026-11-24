"""Markdown link identity, rendered hit targets and terminal hyperlink policy."""

import pytest
from markdown_it import MarkdownIt
from rich.console import Console

from harness import math_markdown
from harness.markdown_links import (
    MarkdownLink, collect_markdown_links, is_web_link, parse_markdown_links,
)
from harness.math_markdown import MathMarkdown


def test_parser_indexes_visible_labels_and_skips_code():
    source = (
        "[**Guide** and `usage`][guide] / [![*alt* text](image.png)](#diagram)\n\n"
        "<https://example.org/a> / `[example](ignored.md)`\n\n"
        "```md\n[example](ignored.md)\n```\n\n"
        "[guide]: <docs/User guide.md#usage> \"Guide title\"\n"
    )
    tokens = MarkdownIt().parse(source)
    assert collect_markdown_links(tokens) == (
        MarkdownLink("Guide and usage", "docs/User%20guide.md#usage"),
        MarkdownLink("alt text", "#diagram"),
        MarkdownLink("https://example.org/a", "https://example.org/a"),
    )
    openers = [c for t in tokens for c in t.children or () if c.type == "link_open"]
    assert [opener.meta["harness_link_index"] for opener in openers] == [0, 1, 2]


def test_bound_links_keep_text_and_inline_styles_with_distinct_actions():
    source = (
        "[**Catalog**](docs.md) [*Contents*](docs.md)\n\n"
        "| Links |\n| --- |\n| [Site](https://example.org/page?q=123#part) |\n"
    )
    markdown = MathMarkdown(source, link_document=14)
    console = Console(width=30, no_color=True)
    segments = list(console.render(markdown))
    spans = {segment.text.strip(): segment.style for segment in segments}
    assert spans["Catalog"].bold and spans["Contents"].italic
    assert spans["Catalog"].meta["@click"] == "app.follow_link(14,0)"
    assert spans["Contents"].meta["@click"] == "app.follow_link(14,1)"
    assert spans["Catalog"].link is spans["Contents"].link is None
    assert spans["Site"].meta["@click"] == "app.follow_link(14,2)"
    assert spans["Site"].link == "https://example.org/page?q=123#part"
    assert "harness link" not in "".join(segment.text for segment in segments)


def test_bare_web_urls_keep_punctuation_outside_and_code_inert_in_all_views():
    from harness.link_screen import _DocumentParser

    url = "https://example.org/$sort$/a_(b)?q=%61%20x#section"
    local = "HTTP://localhost:8080/demo"
    source = (
        f"See ({url}). Then try {local}, or [https://label.example](guide.md).\n\n"
        "`https://example.org/inline` and ftp://example.org/archive\n\n"
        "```text\nhttps://example.org/fenced\n```\n\n"
        "Plain example.org, www.example.org, //example.org, and team@example.org."
    )
    expected = (
        MarkdownLink(url, url), MarkdownLink(local, local), MarkdownLink("https://label.example", "guide.md"),
    )
    bound = MathMarkdown(source, link_document=2)
    assert parse_markdown_links(source) == _DocumentParser(source).links == bound.links == expected
    rendered = list(Console(width=90, no_color=True).render(bound))
    assert any(s.text == url and s.style.meta["@click"] == "app.follow_link(2,0)" for s in rendered)
    assert all(not s.style or "@click" not in s.style.meta for s in rendered if ")." in s.text)


def test_unbound_terminal_preserves_web_targets_and_suppresses_other_schemes():
    markdown = MathMarkdown(
        "[Site](https://example.com/a(b)) [Local](a.md) [Mail](mailto:team@example.com)"
        " ![Image](thing:open)"
    )
    segments = list(Console(width=60).render(markdown))
    urls = [seg.style.link for seg in segments if seg.style and seg.style.link]
    assert urls == ["https://example.com/a(b)"]
    assert all("@click" not in seg.style.meta for seg in segments if seg.style)


def test_disabled_rich_hyperlinks_show_the_authored_destination():
    doc = MathMarkdown("[one](sub%20dir/sample.md)", hyperlinks=False, link_document=3)
    segments = list(Console(width=60).render(doc))
    assert "one (sub%20dir/sample.md)" in "".join(seg.text for seg in segments)
    assert all(not seg.style.link for seg in segments if seg.style)


def test_links_restore_math_labels_and_url_spelling(monkeypatch):
    monkeypatch.setattr(math_markdown, "render_inline_formula_text", lambda source: "x²")
    doc = MathMarkdown("[$x^2$](https://example.org/$sort$/file)", link_document=1)
    assert doc.links == (MarkdownLink("$x^2$", "https://example.org/$sort$/file"),)
    segment = next(seg for seg in Console().render(doc) if seg.text.strip() == "x²")
    assert segment.style.meta["@click"] == "app.follow_link(1,0)"
    assert segment.style.link == doc.links[0].href


def test_display_equation_keeps_its_surrounding_markdown_block():
    markup = (
        "- [$$x$$](#proof)\n\n"
        "| Result |\n| --- |\n| $$y$$ |\n\n"
        "Before\n$$z$$\nAfter"
    )
    prepared, formulas = math_markdown.extract_math(markup)
    x, y, z = formulas
    assert prepared == markup.replace("$$x$$", x).replace("$$y$$", y).replace("$$z$$", f"\n\n{z}\n\n")
    assert MathMarkdown(markup).links == (MarkdownLink("$$x$$", "#proof"),)


@pytest.mark.parametrize("target", [
    "file:///etc/passwd", "mailto:a@b.com", "javascript:alert(1)", "//example.org/path",
    "https:///foo", "https://[bad", "https://good.example:65536/a", "https://a\\b",
    "\nhttps://example.com", "https://example.com/\x1b]8",
])
def test_only_valid_absolute_http_urls_are_terminal_destinations(target):
    assert not is_web_link(target)
