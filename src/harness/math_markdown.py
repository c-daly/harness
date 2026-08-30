"""Rich Markdown with terminal-native, transparently rasterized LaTeX math.

Rich's Markdown renderer intentionally implements ordinary Markdown, not math
extensions.  This module recognizes the conventional ``$...$`` / ``$$...$$``
and ``\\(...\\)`` / ``\\[...\\]`` delimiters outside code spans and fences,
typesets their contents with Matplotlib MathText, and embeds the resulting RGBA
images in the Rich render tree.  The cell renderer preserves transparent pixels
instead of baking in a light or dark background, so the same equation follows
the active Textual theme.

MathText is a deliberately portable TeX-compatible math subset.  A construct it
does not understand is shown verbatim; malformed model output must never make a
completed assistant reply disappear or crash the TUI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from typing import TYPE_CHECKING

from rich.align import Align
from rich.color import Color
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown, MarkdownContext, MarkdownElement
from rich.measure import Measurement
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from PIL import Image


_MARKER_OPEN = "\ue000"
_MARKER_CLOSE = "\ue001"
_MARKER_RE = re.compile(f"({_MARKER_OPEN}\\d+{_MARKER_CLOSE})")
_ALPHA_VISIBLE = 24
_PNG_PADDING_PX = 2


@dataclass(frozen=True)
class Formula:
    source: str
    display: bool
    original: str


class LatexRenderError(ValueError):
    """A formula is outside the supported TeX-compatible MathText subset."""


def _is_escaped(text: str, index: int) -> bool:
    slashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        slashes += 1
        index -= 1
    return bool(slashes % 2)


def _find_closer(text: str, start: int, closer: str, *, multiline: bool) -> int:
    index = start
    while True:
        index = text.find(closer, index)
        if index < 0:
            return -1
        if not multiline and "\n" in text[start:index]:
            return -1
        if not _is_escaped(text, index):
            if closer == "$" and (
                (index > 0 and text[index - 1] == "$")
                or (index + 1 < len(text) and text[index + 1] == "$")
            ):
                index += 1
                continue
            return index
        index += len(closer)


def _fence_at(text: str, index: int) -> tuple[str, int] | None:
    """Return a Markdown fence at a line start (up to three spaces indented)."""
    if index and text[index - 1] != "\n":
        return None
    cursor = index
    while cursor < len(text) and cursor - index < 3 and text[cursor] == " ":
        cursor += 1
    if cursor >= len(text) or text[cursor] not in ("`", "~"):
        return None
    char = text[cursor]
    end = cursor
    while end < len(text) and text[end] == char:
        end += 1
    length = end - cursor
    return (char, length) if length >= 3 else None


def extract_math(markup: str) -> tuple[str, dict[str, Formula]]:
    """Replace math outside Markdown code with private placeholders.

    The returned mapping is insertion ordered.  Escaped dollars, inline code,
    fenced code, incomplete delimiters, and ordinary currency remain byte-for-
    byte unchanged.
    """
    formulas: dict[str, Formula] = {}
    output: list[str] = []
    index = 0
    fence: tuple[str, int] | None = None

    def add_formula(source: str, *, display: bool, original: str) -> str:
        marker = f"{_MARKER_OPEN}{len(formulas)}{_MARKER_CLOSE}"
        formulas[marker] = Formula(source=source.strip(), display=display, original=original)
        return marker

    while index < len(markup):
        possible_fence = _fence_at(markup, index)
        if possible_fence is not None:
            line_end = markup.find("\n", index)
            line_end = len(markup) if line_end < 0 else line_end + 1
            output.append(markup[index:line_end])
            if fence is None:
                fence = possible_fence
            elif possible_fence[0] == fence[0] and possible_fence[1] >= fence[1]:
                fence = None
            index = line_end
            continue
        if fence is not None:
            output.append(markup[index])
            index += 1
            continue

        # Markdown code spans may use any run length of backticks.  Copy the
        # entire span untouched so dollar signs inside code never become math.
        if markup[index] == "`":
            run_end = index
            while run_end < len(markup) and markup[run_end] == "`":
                run_end += 1
            delimiter = markup[index:run_end]
            close = markup.find(delimiter, run_end)
            if close < 0:
                output.append(markup[index:])
                break
            close += len(delimiter)
            output.append(markup[index:close])
            index = close
            continue

        opener = closer = ""
        display = False
        if markup.startswith("$$", index) and not _is_escaped(markup, index):
            opener = closer = "$$"
            display = True
        elif markup[index] == "$" and not _is_escaped(markup, index):
            opener = closer = "$"
        elif markup.startswith(r"\[", index) and not _is_escaped(markup, index):
            opener, closer, display = r"\[", r"\]", True
        elif markup.startswith(r"\(", index) and not _is_escaped(markup, index):
            opener, closer = r"\(", r"\)"

        if opener:
            content_start = index + len(opener)
            # A dollar followed by digits at a word boundary is overwhelmingly
            # currency.  Still permit the unambiguous compact math form `$5$`,
            # but do not let `$10. some prose ...` consume a later dollar from
            # an inline-code span as its closing delimiter.
            if opener == "$" and markup[content_start : content_start + 1].isdigit():
                previous = markup[index - 1] if index else " "
                next_dollar = markup.find("$", content_start)
                whitespace = next(
                    (pos for pos in range(content_start, len(markup)) if markup[pos].isspace()),
                    len(markup),
                )
                if previous.isspace() and (next_dollar < 0 or whitespace < next_dollar):
                    output.append(markup[index])
                    index += 1
                    continue
            close = _find_closer(markup, content_start, closer, multiline=display)
            if close >= 0:
                source = markup[content_start:close]
                after = close + len(closer)
                inline_spacing_ok = display or (
                    bool(source) and not source[0].isspace() and not source[-1].isspace()
                )
                # ``$5 and $10`` is prose about currency, not a math span: the
                # would-be closer is immediately followed by another digit.
                currency_pair = (
                    opener == "$"
                    and source[:1].isdigit()
                    and after < len(markup)
                    and markup[after].isdigit()
                )
                if source.strip() and inline_spacing_ok and not currency_pair:
                    original = markup[index:after]
                    marker = add_formula(source, display=display, original=original)
                    if display:
                        output.append(f"\n\n{marker}\n\n")
                    else:
                        output.append(marker)
                    index = after
                    continue

        output.append(markup[index])
        index += 1

    return "".join(output), formulas


@lru_cache(maxsize=256)
def render_formula_png(
    source: str,
    *,
    color: str,
    font_size: float = 16.0,
    dpi: float = 180.0,
) -> bytes:
    """Typeset one formula to a tightly cropped, transparently padded PNG."""
    import matplotlib as mpl
    from matplotlib.font_manager import FontProperties
    from matplotlib.mathtext import math_to_image
    from PIL import Image

    if not source.strip():
        raise LatexRenderError("empty LaTeX expression")
    raw = BytesIO()
    prop = FontProperties(size=font_size, math_fontfamily="stix")
    try:
        with mpl.rc_context(
            {
                "figure.facecolor": "none",
                "savefig.facecolor": "none",
                "savefig.transparent": True,
                "savefig.pad_inches": 0.0,
                "mathtext.fontset": "stix",
            }
        ):
            math_to_image(f"${source}$", raw, prop=prop, dpi=dpi, format="png", color=color)
        raw.seek(0)
        image = Image.open(raw).convert("RGBA")
        bounds = image.getchannel("A").getbbox()
        if bounds is None:
            raise LatexRenderError("LaTeX expression produced no visible glyphs")
        image = image.crop(bounds)
        padded = Image.new(
            "RGBA",
            (image.width + 2 * _PNG_PADDING_PX, image.height + 2 * _PNG_PADDING_PX),
            (0, 0, 0, 0),
        )
        padded.alpha_composite(image, (_PNG_PADDING_PX, _PNG_PADDING_PX))
        result = BytesIO()
        padded.save(result, format="PNG")
        return result.getvalue()
    except LatexRenderError:
        raise
    except Exception as exc:
        raise LatexRenderError(str(exc)) from exc


class LatexCellImage:
    """A transparent RGBA equation rendered with terminal half-cell glyphs."""

    def __init__(self, formula: Formula, *, color: str) -> None:
        self.formula = formula
        self.color = color

    def _image(self) -> Image.Image:
        from PIL import Image

        png = render_formula_png(self.formula.source, color=self.color)
        return Image.open(BytesIO(png)).convert("RGBA")

    def _size(self, max_width: int) -> tuple[int, int]:
        image = self._image()
        rows = max(2 if self.formula.display else 1, round(image.height / 13))
        rows = min(rows, 8 if self.formula.display else 4)
        width = max(1, round(image.width / image.height * rows * 2))
        if width > max_width:
            rows = max(1, round(rows * max_width / width))
            width = max_width
        return width, rows

    @staticmethod
    def _rgb(pixel: tuple[int, int, int, int]) -> Color:
        return Color.from_rgb(pixel[0], pixel[1], pixel[2])

    @classmethod
    def _segment(
        cls,
        upper: tuple[int, int, int, int],
        lower: tuple[int, int, int, int],
    ) -> Segment:
        upper_visible = upper[3] >= _ALPHA_VISIBLE
        lower_visible = lower[3] >= _ALPHA_VISIBLE
        if upper_visible and lower_visible:
            return Segment("▀", style=Style(color=cls._rgb(upper), bgcolor=cls._rgb(lower)))
        if upper_visible:
            return Segment("▀", style=Style(color=cls._rgb(upper)))
        if lower_visible:
            return Segment("▄", style=Style(color=cls._rgb(lower)))
        return Segment(" ")

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        from PIL import Image

        try:
            image = self._image()
        except LatexRenderError:
            yield Text(self.formula.original)
            return
        width, rows = self._size(max(1, options.max_width))
        scaled = image.resize((width, rows * 2), Image.Resampling.LANCZOS)
        pixels = scaled.load()
        assert pixels is not None
        for row in range(rows):
            for column in range(width):
                yield self._segment(pixels[column, row * 2], pixels[column, row * 2 + 1])
            yield Segment.line()

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        try:
            width, _ = self._size(max(1, options.max_width))
        except LatexRenderError:
            width = len(self.formula.original)
        return Measurement(width, width)


class _MathTextElement(MarkdownElement):
    """Paragraph/heading element capable of holding text and equation images."""

    style_name = "markdown.paragraph"
    justify = "left"

    @classmethod
    def create(cls, markdown: Markdown, token) -> "_MathTextElement":
        assert isinstance(markdown, MathMarkdown)
        return cls(markdown.formulas, markdown.math_color)

    def __init__(self, formulas: dict[str, Formula], color: str) -> None:
        self.formulas = formulas
        self.color = color
        self.parts: list[Text | LatexCellImage] = []
        self._text = Text()

    def on_enter(self, context: MarkdownContext) -> None:
        self.style = context.enter_style(self.style_name)

    def _flush_text(self) -> None:
        if self._text:
            self.parts.append(self._text)
            self._text = Text()

    def on_text(self, context: MarkdownContext, text) -> None:
        if isinstance(text, Text):
            self._text.append_text(text)
            return
        for piece in _MARKER_RE.split(text):
            formula = self.formulas.get(piece)
            if formula is None:
                self._text.append(piece, context.current_style)
            else:
                self._flush_text()
                self.parts.append(LatexCellImage(formula, color=self.color))

    def on_leave(self, context: MarkdownContext) -> None:
        self._flush_text()
        context.leave_style()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        # Rich renders a block element before calling ``on_leave``.  Flush here
        # so text after the final equation (or an all-text block) is present in
        # the render tree.
        self._flush_text()
        if not any(isinstance(part, LatexCellImage) for part in self.parts):
            text = self.parts[0] if self.parts else Text()
            assert isinstance(text, Text)
            text.justify = self.justify
            yield text
            return
        if len(self.parts) == 1 and isinstance(self.parts[0], LatexCellImage):
            yield Align(self.parts[0], align="center" if self.parts[0].formula.display else "left")
            return

        table = Table.grid(padding=0, collapse_padding=True, expand=False)
        for part in self.parts:
            table.add_column(no_wrap=isinstance(part, LatexCellImage))
        table.add_row(*self.parts)
        yield Align(table, align=self.justify)


class _MathHeading(_MathTextElement):
    @classmethod
    def create(cls, markdown: Markdown, token) -> "_MathHeading":
        assert isinstance(markdown, MathMarkdown)
        heading = cls(markdown.formulas, markdown.math_color)
        heading.style_name = f"markdown.{token.tag}"
        heading.justify = "center" if token.tag == "h1" else "left"
        return heading


class MathMarkdown(Markdown):
    """Drop-in Rich Markdown renderable with transparent LaTeX equations."""

    def __init__(self, markup: str, *, color: str = "#f4f4f4", **kwargs) -> None:
        prepared, self.formulas = extract_math(markup)
        self.math_color = color
        # Preserve Rich's native Markdown path byte-for-byte when a reply has
        # no equations.  Only paragraphs containing our private placeholders
        # need the mixed text/image element implementation.
        if self.formulas:
            self.elements = {
                **Markdown.elements,
                "paragraph_open": _MathTextElement,
                "heading_open": _MathHeading,
            }
        super().__init__(prepared, **kwargs)


__all__ = [
    "Formula",
    "LatexRenderError",
    "MathMarkdown",
    "extract_math",
    "render_formula_png",
]
