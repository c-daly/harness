"""Rich Markdown with terminal-native, transparently rasterized LaTeX math.

Rich's Markdown renderer intentionally implements ordinary Markdown, not math
extensions.  This module recognizes the conventional ``$...$`` / ``$$...$$``
and ``\\(...\\)`` / ``\\[...\\]`` delimiters outside code spans and fences,
and typesets their contents with Matplotlib MathText.  Simple one-line
expressions use crisp Unicode glyphs; expressions with two-dimensional layout
use a transparent RGBA image rendered as native Sixel where available, with a
high-density cell fallback.  The same equation therefore follows the active
Textual theme without baking in a light or dark background.

MathText is a deliberately portable TeX-compatible math subset.  A construct it
does not understand is shown verbatim; malformed model output must never make a
completed assistant reply disappear or crash the TUI.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from itertools import count
from typing import TYPE_CHECKING

from rich.align import Align
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown, MarkdownContext, MarkdownElement
from rich.measure import Measurement
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

if TYPE_CHECKING:
    from PIL import Image


_MARKER_OPEN = "\ue000"
_MARKER_CLOSE = "\ue001"
_MARKER_RE = re.compile(f"({_MARKER_OPEN}\\d+{_MARKER_CLOSE})")
_ALPHA_VISIBLE = 48
_PNG_PADDING_PX = 2
_INLINE_DPI = 80.0
_TALL_INLINE_DPI = 120.0
_DISPLAY_DPI = 180.0
_BRAILLE_DOTS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)
_INLINE_PRIMES = {"′", "″", "‴", "⁗"}
_SUPERSCRIPTS = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
_SUBSCRIPTS = str.maketrans("0123456789+-=()aehi jklmnoprstuvx".replace(" ", ""), "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")
_SPACED_OPERATORS = {"+", "=", "×", "÷", "<", ">", "≤", "≥", "→", "←", "↔", "⇒", "⇐", "⇔"}
_SIXEL_RECHECK_SECONDS = 2.0
SIXEL_META_KEY = "harness.sixel"
_SIXEL_PLACEMENT_IDS = count()
_MATRIX_RE = re.compile(
    r"\\begin\{(?P<kind>[pbBvV]?matrix)\}(?P<body>.*?)"
    r"\\end\{(?P=kind)\}",
    re.DOTALL,
)
_UNBRACED_FONT_COMMAND_RE = re.compile(
    r"\\(?P<command>mathcal|mathbb|mathbf|mathrm|mathsf|mathtt|mathit)"
    r"\s+(?P<atom>\\[A-Za-z]+|[A-Za-z0-9])"
)
_TALL_INLINE_RE = re.compile(
    r"\\(?:d?frac|binom|sqrt|sum|prod|int|oint|lim|substack|overset|underset)\b"
)


def _detect_sixel_support() -> bool:
    """Detect Sixel before Textual starts consuming terminal responses.

    tmux already performs the outer-terminal capability negotiation, so its
    per-client flag is both faster and more reliable than issuing a second DA
    query from a pane.  Outside tmux, textual-image performs the query itself.
    Headless and redirected processes always use the Unicode fallback.
    """
    if os.environ.get("HARNESS_MATH_SIXEL", "").lower() in {"0", "false", "off"}:
        return False
    if not sys.__stdout__ or not sys.__stdout__.isatty():
        return False
    if os.environ.get("TMUX"):
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "#{sixel_support}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=0.5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.stdout.strip() == "1"
    try:
        from textual_image.renderable.sixel import query_terminal_support

        return query_terminal_support()
    except Exception:
        return False


# A false result is deliberately not permanent.  Harness may already be
# running when a user enables tmux passthrough / Sixel terminal features, and
# capability detection at module import used to strand that process on the
# expensive Braille fallback until it was restarted.
_SIXEL_AVAILABLE = False
_SIXEL_LAST_CHECK = float("-inf")


def _sixel_available() -> bool:
    global _SIXEL_AVAILABLE, _SIXEL_LAST_CHECK

    if _SIXEL_AVAILABLE:
        return True
    now = time.monotonic()
    if now - _SIXEL_LAST_CHECK < _SIXEL_RECHECK_SECONDS:
        return False
    _SIXEL_LAST_CHECK = now
    _SIXEL_AVAILABLE = _detect_sixel_support()
    return _SIXEL_AVAILABLE


@dataclass(frozen=True)
class Formula:
    source: str
    display: bool
    original: str


@dataclass(frozen=True)
class SixelPlacement:
    """A Textual Sixel widget waiting to be placed over transcript cells."""

    image: Image.Image
    width: int
    rows: int


class LatexRenderError(ValueError):
    """A formula is outside the supported TeX-compatible MathText subset."""


def _normalize_mathtext_source(source: str) -> str:
    """Translate common matrix environments into MathText's supported subset.

    Matplotlib MathText deliberately omits LaTeX environments, but it does
    support ``substack`` and scalable delimiters.  This keeps small matrices
    typeset without requiring a system TeX installation.
    """

    # Display math is commonly formatted across source lines for readability,
    # but MathText only accepts a single logical line.  TeX treats those source
    # newlines as ordinary whitespace; do the same while preserving explicit
    # matrix row separators (``\\``).
    source = re.sub(r"\s+", " ", source.strip())
    # Full LaTeX lets font commands consume an unbraced next atom (``\mathbf
    # v``), while MathText requires braces.  Models commonly emit the valid
    # compact form, so adapt it rather than exposing the delimiters verbatim.
    source = _UNBRACED_FONT_COMMAND_RE.sub(
        lambda match: rf"\{match.group('command')}{{{match.group('atom')}}}",
        source,
    )
    # MathText spells the double vertical delimiter ``\Vert`` and does not
    # recognize LaTeX's left/right aliases.
    source = source.replace(r"\lVert", r"\Vert").replace(r"\rVert", r"\Vert")

    delimiters = {
        "matrix": ("", ""),
        "pmatrix": ("(", ")"),
        "bmatrix": ("[", "]"),
        "Bmatrix": (r"\{", r"\}"),
        "vmatrix": ("|", "|"),
        "Vmatrix": (r"\Vert", r"\Vert"),
    }

    def replace(match: re.Match[str]) -> str:
        rows = []
        for row in re.split(r"\\\\", match.group("body")):
            cells = [cell.strip() for cell in row.split("&")]
            if any(cells):
                rows.append(r"\quad ".join(cells))
        if not rows:
            return match.group(0)
        body = r" \\ ".join(rows)
        left, right = delimiters[match.group("kind")]
        stack = rf"\substack{{{body}}}"
        return rf"\left{left}{stack}\right{right}" if left else stack

    return _MATRIX_RE.sub(replace, source)


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


def _fence_at(text: str, index: int) -> tuple[str, int, bool] | None:
    """Return (char, length, may_close) for a fence at a line start.

    Indented up to three spaces, per Markdown.  ``may_close`` is False when the
    run is followed by anything other than whitespace: CommonMark permits an
    info string on an OPENING fence only, so ```` ```text hello ```` can open a
    block but never close one.  Treating it as a close would end the block
    early and expose the code inside it to the math scanner.
    """
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
    if length < 3:
        return None
    line_end = text.find("\n", end)
    line_end = len(text) if line_end < 0 else line_end
    info = text[end:line_end]
    if char == "`" and "`" in info:
        # CommonMark: a backtick fence's info string may not contain a
        # backtick, so such a line is not a fence -- it can neither open nor
        # close.  extract_math's output is unchanged either way (a rejected
        # line falls through to the code-span branch, which consumes the same
        # span); this keeps the helper honest for any future caller.
        return None
    return (char, length, not info.strip())


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
            char, length, may_close = possible_fence
            if fence is None:
                fence = (char, length)
            elif may_close and char == fence[0] and length >= fence[1]:
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
            normalized = _normalize_mathtext_source(source)
            math_to_image(
                f"${normalized}$", raw, prop=prop, dpi=dpi, format="png", color=color
            )
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


@lru_cache(maxsize=256)
def render_inline_formula_text(source: str) -> str | None:
    """Return a crisp one-line Unicode form for terminal-safe MathText.

    Baseline glyphs are preserved directly. Simple raised and lowered glyphs
    are translated to Unicode superscripts and subscripts; expressions that
    require true two-dimensional layout continue through the image renderer.
    """
    from matplotlib.font_manager import FontProperties
    from matplotlib.mathtext import MathTextParser

    try:
        parsed = MathTextParser("path").parse(
            "$" + source + "$",
            dpi=100,
            prop=FontProperties(size=16, math_fontfamily="stix"),
        )
    except Exception:
        return None
    if parsed.rects or not parsed.glyphs:
        return None

    glyphs: list[tuple[float, float, float, str]] = []
    for _, font_size, codepoint, _, x, y in parsed.glyphs:
        try:
            character = chr(codepoint)
        except (OverflowError, ValueError):
            return None
        if not character.isprintable():
            return None
        glyphs.append((float(x), float(y), float(font_size), character))

    full_size = max(glyph[2] for glyph in glyphs)
    baseline_glyphs = [
        glyph
        for glyph in glyphs
        if glyph[2] >= full_size - 1 and glyph[3] not in _INLINE_PRIMES
    ]
    if not baseline_glyphs:
        return None
    baseline = sum(glyph[1] for glyph in baseline_glyphs) / len(baseline_glyphs)
    if max(glyph[1] for glyph in baseline_glyphs) - min(
        glyph[1] for glyph in baseline_glyphs
    ) > 2:
        return None

    output: list[str] = []
    for _, y, font_size, character in sorted(glyphs):
        if character not in _INLINE_PRIMES and font_size < full_size - 1:
            table = (
                _SUPERSCRIPTS
                if y > baseline + 2
                else _SUBSCRIPTS
                if y < baseline - 2
                else None
            )
            if table is None:
                return None
            translated = character.translate(table)
            if translated == character:
                return None
            character = translated
        if character in _SPACED_OPERATORS:
            if output and not output[-1].endswith(" "):
                output.append(" ")
            output.extend((character, " "))
        elif character == ",":
            output.extend((character, " "))
        else:
            output.append(character)
    return "".join(output).strip()


class LatexCellImage:
    """A transparent equation rendered as Sixel or high-density Braille."""

    def __init__(
        self,
        formula: Formula,
        *,
        color: str,
        sixel_placements: dict[str, SixelPlacement] | None = None,
    ) -> None:
        self.formula = formula
        self.color = color
        self.sixel_placements = sixel_placements
        self._cached_image: Image.Image | None = None
        self._placement_id = str(next(_SIXEL_PLACEMENT_IDS))

    def _inline_rows(self) -> int:
        """Use a second cell only for notation with genuinely stacked layout."""
        return 2 if _TALL_INLINE_RE.search(self.formula.source) else 1

    def _image(self) -> Image.Image:
        from PIL import Image

        if self._cached_image is None:
            # Render inline notation close to its final one-cell pixel height.
            # Rendering it at display resolution and then shrinking it by more
            # than 3x makes thin strokes visibly soft.  Standalone equations
            # retain the higher source resolution used for their larger boxes.
            inline_dpi = _TALL_INLINE_DPI if self._inline_rows() == 2 else _INLINE_DPI
            png = render_formula_png(
                self.formula.source,
                color=self.color,
                dpi=_DISPLAY_DPI if self.formula.display else inline_dpi,
            )
            self._cached_image = Image.open(BytesIO(png)).convert("RGBA")
        return self._cached_image

    def _size(self, max_width: int, image: Image.Image | None = None) -> tuple[int, int]:
        image = image or self._image()
        # Ordinary inline scripts share one terminal row with prose. Stacked
        # notation gets two rows: enough to keep fractions legible without
        # returning to the oversized three-row inline layout.
        rows = (
            min(7, max(2, round(image.height / 14)))
            if self.formula.display
            else self._inline_rows()
        )
        width = max(1, round(image.width / image.height * rows * 2))
        if width > max_width:
            rows = max(1, round(rows * max_width / width))
            width = max_width
        return width, rows

    def _segment(self, alpha, column: int, row: int) -> Segment:
        dots = 0
        for y, dot_row in enumerate(_BRAILLE_DOTS):
            for x, dot in enumerate(dot_row):
                if alpha.getpixel((column * 2 + x, row * 4 + y)) >= _ALPHA_VISIBLE:
                    dots |= dot
        if not dots:
            return Segment(" ")
        return Segment(chr(0x2800 + dots), style=Style(color=self.color))

    def _sixel(self, image: Image.Image, width: int, rows: int):
        from textual_image.renderable.sixel import Image as SixelImage, SixelOptions

        # Equations contain one foreground color plus antialiasing.  A small
        # palette preserves those edges while avoiding the default 256-color
        # quantization and its much larger terminal payload.
        return SixelImage(
            image,
            width=width,
            height=rows,
            sixel_options=SixelOptions(colors=16),
        )

    def _sixel_placeholder(self, image: Image.Image, width: int, rows: int) -> RenderResult:
        assert self.sixel_placements is not None
        self.sixel_placements[self._placement_id] = SixelPlacement(image, width, rows)
        marker_style = Style(meta={SIXEL_META_KEY: self._placement_id})
        for _ in range(rows):
            yield Segment(" " * width, marker_style)
            yield Segment.line()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        from PIL import Image

        try:
            image = self._image()
        except LatexRenderError:
            yield Text(self.formula.original)
            return
        width, rows = self._size(max(1, options.max_width), image)
        if _sixel_available() and not console.no_color:
            if self.sixel_placements is not None:
                yield from self._sixel_placeholder(image, width, rows)
                return
            if self.formula.display:
                yield from console.render(self._sixel(image, width, rows), options)
                return
        scaled = image.resize((width * 2, rows * 4), Image.Resampling.LANCZOS)
        alpha = scaled.getchannel("A")
        for row in range(rows):
            for column in range(width):
                yield self._segment(alpha, column, row)
            yield Segment.line()

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        try:
            width, _ = self._size(max(1, options.max_width))
        except LatexRenderError:
            width = len(self.formula.original)
        return Measurement(width, width)


@dataclass(frozen=True)
class _FlowImage:
    renderable: LatexCellImage
    width: int
    rows: int


class _MathTextElement(MarkdownElement):
    """Paragraph/heading element capable of holding text and equation images."""

    style_name = "markdown.paragraph"
    justify = "left"

    @classmethod
    def create(cls, markdown: Markdown, token) -> "_MathTextElement":
        assert isinstance(markdown, MathMarkdown)
        return cls(markdown.formulas, markdown.math_color, markdown.sixel_placements)

    def __init__(
        self,
        formulas: dict[str, Formula],
        color: str,
        sixel_placements: dict[str, SixelPlacement] | None,
    ) -> None:
        self.formulas = formulas
        self.color = color
        self.sixel_placements = sixel_placements
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
                inline_text = render_inline_formula_text(formula.source)
                if inline_text is not None:
                    if formula.display:
                        self._flush_text()
                        display_text = Text(inline_text, Style(color=self.color))
                        display_text.justify = "center"
                        self.parts.append(display_text)
                    else:
                        self._text.append(inline_text, Style(color=self.color))
                else:
                    self._flush_text()
                    promoted = not formula.display
                    if promoted:
                        # A terminal cell cannot make a two-dimensional inline
                        # raster both compact and legible. Promote it to a
                        # deliberate display equation between hard line breaks.
                        self.parts.append(Text("\n"))
                        formula = Formula(
                            source=formula.source,
                            display=True,
                            original=formula.original,
                        )
                    self.parts.append(
                        LatexCellImage(
                            formula,
                            color=self.color,
                            sixel_placements=self.sixel_placements,
                        )
                    )
                    if promoted:
                        self.parts.append(Text("\n"))

    def on_leave(self, context: MarkdownContext) -> None:
        self._flush_text()
        context.leave_style()

    @staticmethod
    def _text_tokens(text: Text) -> list[Text]:
        """Split styled prose into wrap-safe words, spaces, and hard breaks."""
        return [
            text[match.start() : match.end()]
            for match in re.finditer(r"\n|[^\s\n]+[^\S\n]*|[^\S\n]+", text.plain)
        ]

    def _flow_rows(
        self, console: Console, max_width: int
    ) -> list[list[Text | _FlowImage]]:
        """Lay mixed prose and equation boxes out in document order."""
        rows: list[list[Text | _FlowImage]] = []
        row: list[Text | _FlowImage] = []
        used = 0

        def flush() -> None:
            nonlocal row, used
            if row:
                rows.append(row)
                row = []
                used = 0

        def append_text(token: Text) -> None:
            nonlocal row, used
            if token.plain == "\n":
                flush()
                return
            if not token.plain:
                return
            if not row and token.plain.isspace():
                return
            token_width = token.cell_len
            if token_width > max_width:
                flush()
                wrapped = token.wrap(console, max_width, overflow="fold")
                for index, line in enumerate(wrapped):
                    if not line:
                        continue
                    if index == len(wrapped) - 1 and line.cell_len < max_width:
                        row = [line]
                        used = line.cell_len
                    else:
                        rows.append([line])
                return
            if row and used + token_width > max_width:
                flush()
                if token.plain.isspace():
                    return
            if row and isinstance(row[-1], Text):
                row[-1].append_text(token)
            else:
                row.append(token.copy())
            used += token_width

        for part in self.parts:
            if isinstance(part, Text):
                for token in self._text_tokens(part):
                    append_text(token)
                continue
            try:
                width, image_rows = part._size(max_width)
            except LatexRenderError:
                append_text(Text(part.formula.original, Style(color=self.color)))
                continue
            if row and used + width > max_width:
                flush()
            row.append(_FlowImage(part, width, image_rows))
            used += width
        flush()
        return rows

    @staticmethod
    def _render_flow_row(
        row: list[Text | _FlowImage],
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        height = max(item.rows if isinstance(item, _FlowImage) else 1 for item in row)
        rendered: list[tuple[list[list[Segment]], int, int]] = []
        for item in row:
            if isinstance(item, _FlowImage):
                width = item.width
                item_height = item.rows
                lines = console.render_lines(
                    item.renderable,
                    options.update_width(width),
                    pad=True,
                )
            else:
                width = item.cell_len
                item_height = 1
                lines = console.render_lines(
                    item,
                    options.update_width(max(1, width)),
                    pad=True,
                )
            # Put one-line prose on the lower row beside a two-row fraction,
            # where the equation's visual baseline sits. Odd-height rows keep
            # their ordinary centered alignment.
            top = (height - item_height + 1) // 2
            rendered.append((lines, width, top))

        for line_index in range(height):
            for lines, width, top in rendered:
                item_line = line_index - top
                if 0 <= item_line < len(lines):
                    yield from lines[item_line]
                else:
                    yield Segment(" " * width)
            yield Segment.line()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        # Rich renders a block element before calling ``on_leave``.  Flush here
        # so text after the final equation (or an all-text block) is present in
        # the render tree.
        self._flush_text()
        if not any(isinstance(part, LatexCellImage) for part in self.parts):
            text = Text()
            for part in self.parts:
                assert isinstance(part, Text)
                text.append_text(part)
            if len(self.parts) == 1 and isinstance(self.parts[0], Text):
                text.justify = self.parts[0].justify or self.justify
            else:
                text.justify = self.justify
            yield text
            return
        if len(self.parts) == 1 and isinstance(self.parts[0], LatexCellImage):
            yield Align(self.parts[0], align="center" if self.parts[0].formula.display else "left")
            return
        for row in self._flow_rows(console, max(1, options.max_width)):
            yield from self._render_flow_row(row, console, options)


class _MathHeading(_MathTextElement):
    @classmethod
    def create(cls, markdown: Markdown, token) -> "_MathHeading":
        assert isinstance(markdown, MathMarkdown)
        heading = cls(markdown.formulas, markdown.math_color, markdown.sixel_placements)
        heading.style_name = f"markdown.{token.tag}"
        heading.justify = "center" if token.tag == "h1" else "left"
        return heading


class MathMarkdown(Markdown):
    """Drop-in Rich Markdown renderable with transparent LaTeX equations."""

    def __init__(
        self,
        markup: str,
        *,
        color: str = "#f4f4f4",
        sixel_widgets: bool = False,
        **kwargs,
    ) -> None:
        prepared, self.formulas = extract_math(markup)
        self.math_color = color
        self.sixel_placements: dict[str, SixelPlacement] | None = (
            {} if sixel_widgets else None
        )
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
    "SIXEL_META_KEY",
    "SixelPlacement",
    "extract_math",
    "render_inline_formula_text",
    "render_formula_png",
]
