"""LaTeX-aware Markdown rendering for the terminal transcript."""

from io import BytesIO

import pytest
from PIL import Image
from rich.console import Console

import harness.math_markdown as math_markdown
from harness.math_markdown import (
    LatexRenderError,
    MathMarkdown,
    extract_math,
    render_formula_png,
    render_inline_formula_text,
)


def test_extracts_inline_and_display_math_without_touching_markdown():
    prepared, formulas = extract_math(
        "Euler says $e^{i\\pi} + 1 = 0$.\n\n$$\\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}$$"
    )

    assert [formula.source for formula in formulas.values()] == [
        r"e^{i\pi} + 1 = 0",
        r"\frac{-b \pm \sqrt{b^2-4ac}}{2a}",
    ]
    assert [formula.display for formula in formulas.values()] == [False, True]
    assert "Euler says" in prepared
    assert all(marker in prepared for marker in formulas)


def test_math_scanner_preserves_code_escaped_dollars_and_currency():
    source = (
        "Cost is $5 and $10. Escaped \\$x\\$ stays literal. "
        "Inline code `$x^2$` stays code.\n\n"
        "```python\nprice = '$20'\nformula = '$x^2$'\n```"
    )

    prepared, formulas = extract_math(source)

    assert formulas == {}
    assert prepared == source


def test_render_formula_png_has_a_real_transparent_alpha_channel():
    png = render_formula_png(r"\frac{1}{\sqrt{2\pi}} e^{-x^2/2}", color="#f4f4f4")
    image = Image.open(BytesIO(png)).convert("RGBA")

    alpha = image.getchannel("A")
    assert alpha.getextrema()[0] == 0  # transparent background exists
    assert alpha.getextrema()[1] > 0  # visible equation glyphs exist
    assert image.getpixel((0, 0))[3] == 0  # transparent padding is guaranteed


def test_math_markdown_renders_typeset_cells_and_keeps_surrounding_text():
    renderable = MathMarkdown(r"Before $\frac{x}{y}$ after", color="#ffffff")
    console = Console(width=80, record=True, force_terminal=True, color_system="truecolor")

    console.print(renderable)
    rendered = console.export_text()

    assert "Before" in rendered
    assert "after" in rendered
    assert r"\frac" not in rendered  # the source was replaced by the typeset image
    assert any(0x2800 < ord(glyph) <= 0x28FF for glyph in rendered)
    assert not any(glyph in rendered for glyph in ("▀", "▄", "█"))


def test_real_reply_formulas_use_dense_cells_without_block_art():
    source = (
        "1. The increment \\(\\Delta x\\), deriving the derivative as\n"
        "\\[f'(x)=\\lim_{\\Delta x\\to0}"
        "\\frac{f(x+\\Delta x)-f(x)}{\\Delta x}\\]\n"
        "2. The Dirac delta \\(\\delta(x)\\), deriving its derivative "
        "\\(\\delta'(x)\\)."
    )
    console = Console(width=120, record=True, force_terminal=True, color_system="truecolor")

    console.print(MathMarkdown(source, color="#ffffff"))
    rendered = console.export_text()

    assert "The increment" in rendered
    assert "Dirac delta" in rendered
    assert "Δx" in rendered
    assert "δ(x)" in rendered
    assert "δ′(x)" in rendered
    assert sum(0x2800 < ord(glyph) <= 0x28FF for glyph in rendered) >= 20
    assert not any(glyph in rendered for glyph in ("▀", "▄", "█"))


def test_display_math_uses_native_transparent_sixel_when_available(monkeypatch):
    monkeypatch.setattr(math_markdown, "_SIXEL_AVAILABLE", True)
    renderable = MathMarkdown(r"\[\frac{x}{y}\]", color="#ffffff")
    console = Console(
        width=80,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
    )

    segments = list(console.render(renderable, console.options))
    sixel = "".join(segment.text for segment in segments if segment.control)

    assert "\x1bP0;1;0q" in sixel  # P2=1 requests a transparent background
    assert not any(0x2800 < ord(glyph) <= 0x28FF for glyph in sixel)


def test_no_color_output_uses_safe_unicode_fallback(monkeypatch):
    monkeypatch.setattr(math_markdown, "_SIXEL_AVAILABLE", True)
    renderable = MathMarkdown(r"\[\frac{x}{y}\]", color="#ffffff")
    console = Console(width=80, no_color=True)

    segments = list(console.render(renderable, console.options))
    rendered = "".join(segment.text for segment in segments)

    assert "\x1bP" not in rendered
    assert any(0x2800 < ord(glyph) <= 0x28FF for glyph in rendered)


def test_simple_inline_math_uses_crisp_unicode_but_layout_stays_rasterized():
    assert render_inline_formula_text(r"\Delta x") == "Δx"
    assert render_inline_formula_text(r"\delta(x)") == "δ(x)"
    assert render_inline_formula_text(r"\delta'(x)") == "δ′(x)"
    assert render_inline_formula_text(r"\alpha + \beta = \gamma") == "α + β = γ"
    assert render_inline_formula_text(r"x^2") is None
    assert render_inline_formula_text(r"\frac{x}{y}") is None


def test_invalid_latex_falls_back_to_source_instead_of_crashing():
    source = r"Broken: $\frac{unclosed$"
    renderable = MathMarkdown(source, color="#ffffff")
    console = Console(width=80, record=True, force_terminal=True, color_system="truecolor")

    console.print(renderable)

    assert r"\frac{unclosed" in console.export_text()


def test_direct_renderer_reports_invalid_latex():
    with pytest.raises(LatexRenderError):
        render_formula_png(r"\frac{unclosed", color="#ffffff")
