from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont


def wrap_words(words: list[str], font: ImageFont.FreeTypeFont, max_width: float) -> list[list[str]]:
    """Greedy word-wrap using real glyph widths for the given font."""
    rows: list[list[str]] = []
    current: list[str] = []

    for word in words:
        candidate = current + [word]
        width = font.getlength(" ".join(candidate))
        if width > max_width and current:
            rows.append(current)
            current = [word]
        else:
            current = candidate

    if current:
        rows.append(current)
    return rows


def layout_words(
    words: list[str], font_path: str, font_size: int, max_width: int, line_height: int
) -> list[dict]:
    """Compute each word's (text, row, x, y) box, top-left origin, rows centered horizontally."""
    font = ImageFont.truetype(font_path, font_size)
    rows = wrap_words(words, font, max_width)
    space_width = font.getlength(" ")

    layout = []
    for row_idx, row_words in enumerate(rows):
        row_width = sum(font.getlength(w) for w in row_words) + space_width * (len(row_words) - 1)
        x = (max_width - row_width) / 2
        y = row_idx * line_height
        for word in row_words:
            word_width = font.getlength(word)
            layout.append({"text": word, "row": row_idx, "x": x, "y": y, "width": word_width})
            x += word_width + space_width

    return layout


def block_height(layout: list[dict], line_height: int) -> int:
    if not layout:
        return 0
    return (max(item["row"] for item in layout) + 1) * line_height


def render_caption_frame(
    layout: list[dict],
    active_index: int | None,
    font_path: str,
    font_size: int,
    canvas_size: tuple[int, int],
    base_color: tuple[int, int, int],
    highlight_color: tuple[int, int, int],
) -> Image.Image:
    """Draw every word onto one transparent canvas, one call per row, with the active word recolored.

    Rendering full rows (not one TextClip per word) avoids MoviePy's TextClip mis-sizing some
    short words when many differently-sized TextClips are composited together in the same frame.
    """
    font = ImageFont.truetype(font_path, font_size)
    img = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    rows: dict[int, list[tuple[int, dict]]] = {}
    for i, item in enumerate(layout):
        rows.setdefault(item["row"], []).append((i, item))

    for row_items in rows.values():
        for i, item in row_items:
            color = highlight_color if i == active_index else base_color
            draw.text((item["x"], item["y"]), item["text"], font=font, fill=(*color, 255))

    return img
