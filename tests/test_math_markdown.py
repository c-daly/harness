"""LaTeX-aware Markdown rendering for the terminal transcript."""

from io import BytesIO

import pytest
from PIL import Image
from rich.console import Console

from harness.math_markdown import (
    LatexRenderError,
    MathMarkdown,
    extract_math,
    render_formula_png,
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
    renderable = MathMarkdown("Before $x^2 + y^2 = z^2$ after", color="#ffffff")
    console = Console(width=80, record=True, force_terminal=True, color_system="truecolor")

    console.print(renderable)
    rendered = console.export_text()

    assert "Before" in rendered
    assert "after" in rendered
    assert "x^2" not in rendered  # the source was replaced by the typeset image
    assert any(glyph in rendered for glyph in ("▀", "▄", "█"))


def test_invalid_latex_falls_back_to_source_instead_of_crashing():
    source = r"Broken: $\frac{unclosed$"
    renderable = MathMarkdown(source, color="#ffffff")
    console = Console(width=80, record=True, force_terminal=True, color_system="truecolor")

    console.print(renderable)

    assert r"\frac{unclosed" in console.export_text()


def test_direct_renderer_reports_invalid_latex():
    with pytest.raises(LatexRenderError):
        render_formula_png(r"\frac{unclosed", color="#ffffff")
