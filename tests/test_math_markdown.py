"""LaTeX-aware Markdown rendering for the terminal transcript."""

import re
from io import BytesIO

import pytest
from PIL import Image
from rich.console import Console

import harness.math_markdown as math_markdown
from harness.math_markdown import (
    Formula,
    LatexCellImage,
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


@pytest.mark.parametrize("formula", [
    r"1.898 \times 10^{27}", r"2 + 2 = 4", r"2 + 2", r"2x + 1",
    r"10^{-3} \mathrm{kg}", r"5",
])
def test_number_leading_inline_math_is_not_mistaken_for_currency(formula):
    prepared, formulas = extract_math(f"The value is ${formula}$ today.")
    assert [item.source for item in formulas.values()] == [formula]
    assert "The value is " in prepared and " today." in prepared


@pytest.mark.parametrize("source", [
    r"Cost is $5 and $10. Use `$2 + 2$` for math.",
    r"Cost is $10. Use `\times` then `$x^2$`.",
    r"Cost is $10/item and $20/item.",
    r"The mass is $1.898 \times 10^{27}% kg.",
])
def test_numeric_math_detection_preserves_currency_code_and_incomplete_spans(source):
    prepared, formulas = extract_math(source)
    assert formulas == {}
    assert prepared == source


def test_currency_does_not_consume_a_later_numeric_formula():
    prepared, formulas = extract_math(r"Cost is $10. The mass is $1.898 \times 10^{27}$ kg.")
    assert [item.source for item in formulas.values()] == [r"1.898 \times 10^{27}"]
    assert "Cost is $10. The mass is " in prepared


def test_fence_line_with_trailing_content_does_not_close_a_code_block():
    """CommonMark allows an info string on an OPENING fence only, so a line
    like ```text hello inside a block continues it. Closing early would let
    the math scanner rewrite dollar-delimited code that follows."""
    source = (
        "```\n"
        "```text hello\n"
        "price = '$20'\n"
        "formula = '$x^2$'\n"
        "```\n"
    )

    prepared, formulas = extract_math(source)

    assert formulas == {}
    assert prepared == source


def test_unmatched_backtick_run_does_not_stop_later_math():
    """CommonMark: an unmatched backtick run is literal text and the rest of
    the content is parsed normally. Copying the remainder and stopping left
    every later formula in the reply unrendered."""
    source = "```js `x`\nprose after\n\n$x^2$ is math.\n"

    _prepared, formulas = extract_math(source)

    assert len(formulas) == 1
    assert next(iter(formulas.values())).source == "x^2"


def test_currency_after_an_unmatched_backtick_run_stays_literal():
    """Resuming the scan must not resurrect the currency false positive."""
    source = "Use `echo to print. Cost is $5 and $10.\n"

    prepared, formulas = extract_math(source)

    assert formulas == {}
    assert prepared == source


def test_fence_helper_rejects_a_backtick_fence_with_backticks_in_its_info():
    """CommonMark: a backtick fence's info string may not contain a backtick,
    so such a line is not a fence at all -- it can neither open nor close.

    extract_math's output is unchanged either way: a rejected line falls
    through to the code-span branch, which consumes the same span. This keeps
    the helper honest for any future caller rather than fixing a live defect.
    """
    from harness.math_markdown import _fence_at

    assert _fence_at("```js `x`\n", 0) is None
    assert _fence_at("``` `\n", 0) is None
    # a plain info string is a valid opening fence that may not close
    assert _fence_at("```js\n", 0) == ("`", 3, False)
    # bare fence: opens or closes
    assert _fence_at("```\n", 0) == ("`", 3, True)


def test_tilde_fence_may_carry_backticks_in_its_info_string():
    """The backtick restriction is specific to backtick fences."""
    source = "~~~js `x`\nprice = '$20'\n~~~\n"

    prepared, formulas = extract_math(source)

    assert formulas == {}
    assert prepared == source


def test_matching_length_bare_fence_still_closes_a_code_block():
    source = "```\ncode with $5 and $10\n```\n\nProse $x^2$ is math.\n"

    prepared, formulas = extract_math(source)

    assert len(formulas) == 1
    assert next(iter(formulas.values())).source == "x^2"
    assert "code with $5 and $10" in prepared


def test_render_formula_png_has_a_real_transparent_alpha_channel():
    png = render_formula_png(r"\frac{1}{\sqrt{2\pi}} e^{-x^2/2}", color="#f4f4f4")
    image = Image.open(BytesIO(png)).convert("RGBA")

    alpha = image.getchannel("A")
    assert alpha.getextrema()[0] == 0  # transparent background exists
    assert alpha.getextrema()[1] > 0  # visible equation glyphs exist
    assert image.getpixel((0, 0))[3] == 0  # transparent padding is guaranteed


def test_render_formula_png_supports_a_common_latex_matrix_environment():
    png = render_formula_png(
        "\\det\n\\begin{pmatrix}\na & b\\\\\nc & d\n\\end{pmatrix}\n=ad-bc",
        color="#f4f4f4",
    )
    image = Image.open(BytesIO(png)).convert("RGBA")

    assert image.width > image.height
    assert image.getchannel("A").getbbox() is not None


def test_render_formula_png_accepts_common_unbraced_font_commands_and_norm():
    sources = (
        r"X\sim\mathcal N(\mu,\sigma^2)",
        r"\mathbb E[X]=\mu",
        r"\mathbf v=(v_1,\ldots,v_n)",
        r"\lVert\mathbf v\rVert_2=\sqrt{\sum_{i=1}^n v_i^2}",
    )

    for source in sources:
        image = Image.open(BytesIO(render_formula_png(source, color="#f4f4f4")))
        assert image.getchannel("A").getbbox() is not None


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


def test_sixel_support_is_rechecked_after_an_initial_miss(monkeypatch):
    checks = iter((False, True))
    monkeypatch.setattr(math_markdown, "_SIXEL_AVAILABLE", False)
    monkeypatch.setattr(math_markdown, "_SIXEL_LAST_CHECK", float("-inf"))
    monkeypatch.setattr(math_markdown, "_SIXEL_RECHECK_SECONDS", 0)
    monkeypatch.setattr(math_markdown, "_detect_sixel_support", lambda: next(checks))

    assert math_markdown._sixel_available() is False
    assert math_markdown._sixel_available() is True
    assert math_markdown._sixel_available() is True


def test_equation_image_is_decoded_only_once(monkeypatch):
    cell = LatexCellImage(
        Formula(source=r"\frac{x}{y}", display=True, original=r"\[\frac{x}{y}\]"),
        color="#ffffff",
    )
    png = render_formula_png(r"\frac{x}{y}", color="#ffffff")
    calls = 0

    def render_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        return png

    monkeypatch.setattr(math_markdown, "render_formula_png", render_once)

    assert cell._image() is cell._image()
    assert calls == 1


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
    assert render_inline_formula_text(r"x^2") == "x²"
    assert render_inline_formula_text(r"x_1") == "x₁"
    assert render_inline_formula_text(r"x_i^2") == "xᵢ²"
    assert render_inline_formula_text(r"\frac{x}{y}") is None


def test_inline_raster_uses_two_rows_only_for_stacked_notation():
    compact_inline = LatexCellImage(
        Formula(source=r"x^2", display=False, original=r"\(x^2\)"),
        color="#ffffff",
    )
    stacked_inline = LatexCellImage(
        Formula(source=r"\frac{x}{y}", display=False, original=r"\(\frac{x}{y}\)"),
        color="#ffffff",
    )
    display = LatexCellImage(
        Formula(source=r"\frac{x}{y}", display=True, original=r"\[\frac{x}{y}\]"),
        color="#ffffff",
    )

    assert compact_inline._size(80)[1] == 1
    assert stacked_inline._size(80)[1] == 2
    assert display._size(80)[1] >= 2
    assert compact_inline._image().height < stacked_inline._image().height
    assert stacked_inline._image().height < display._image().height


def test_simple_display_math_uses_centered_unicode_without_rasterizing(monkeypatch):
    def unexpected_raster(*args, **kwargs):
        raise AssertionError("simple display math should not be rasterized")

    monkeypatch.setattr(math_markdown, "render_formula_png", unexpected_raster)
    renderable = MathMarkdown(r"\[(G-F)'=0\]", color="#ffffff")
    console = Console(width=40, record=True, force_terminal=True, no_color=False)

    console.print(renderable)
    rendered = console.export_text()

    assert "(G−F)′ = 0" in rendered
    assert not any(0x2800 < ord(glyph) <= 0x28FF for glyph in rendered)


@pytest.mark.parametrize("terminal", ["dumb", "xterm-256color"])
@pytest.mark.parametrize("width", [12, 28, 40, 78])
@pytest.mark.parametrize("backend", ["braille", "sixel"])
def test_complex_inline_math_wraps_with_prose_in_document_order(monkeypatch, terminal, width, backend):
    source = (
        r"For the quadratic equation \(ax^2+bx+c=0\), the solutions are "
        r"\(x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}\). In calculus, the Gaussian "
        r"integral satisfies \(\int_{-\infty}^{\infty}e^{-x^2}\,dx=\sqrt{\pi}\)."
    )
    # Rich's TERM=dumb fallback ignores a width-only override. Supplying both
    # dimensions ensures this test actually exercises the requested geometry.
    console = Console(width=width, height=25, force_terminal=True, no_color=False,
                      _environ={"TERM": terminal})
    assert console.options.max_width == width
    monkeypatch.setattr(math_markdown, "_sixel_available", lambda: backend == "sixel")
    renderable = MathMarkdown(source, color="#ffffff", sixel_widgets=backend == "sixel")

    lines = console.render_lines(renderable, console.options)
    rendered = "\n".join("".join(segment.text for segment in line) for line in lines)
    prose = re.sub(r"[\u2800-\u28ff]", "", rendered)
    prose = " ".join(prose.split())

    phrases = (
        "For the quadratic equation",
        "the solutions are",
        "In calculus",
        "the Gaussian integral satisfies",
    )
    positions = [prose.index(phrase) for phrase in phrases]
    assert positions == sorted(positions)
    assert all(sum(segment.cell_length for segment in line) <= width for line in lines)
    if backend == "sixel":
        assert len(renderable.sixel_placements) == 2
        assert all(placement.width <= width for placement in renderable.sixel_placements.values())
    else:
        assert any(0x2800 < ord(char) <= 0x28FF for char in rendered)


def test_invalid_latex_falls_back_to_source_instead_of_crashing():
    source = r"Broken: $\frac{unclosed$"
    renderable = MathMarkdown(source, color="#ffffff")
    console = Console(width=80, record=True, force_terminal=True, color_system="truecolor")

    console.print(renderable)

    assert r"\frac{unclosed" in console.export_text()


def test_direct_renderer_reports_invalid_latex():
    with pytest.raises(LatexRenderError):
        render_formula_png(r"\frac{unclosed", color="#ffffff")
