from __future__ import annotations

from PIL import ImageFont

from radar.captions import block_height, layout_words, render_caption_frame, wrap_words

FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_SIZE = 52
LINE_HEIGHT = 80


class TestWrapWords:
    def test_short_words_stay_on_one_row(self):
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
        rows = wrap_words(["a", "short", "line"], font, max_width=900)
        assert rows == [["a", "short", "line"]]

    def test_wraps_when_row_exceeds_max_width(self):
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
        words = ["This", "is", "a", "long", "enough", "sentence", "to", "wrap", "onto", "multiple", "rows"]
        rows = wrap_words(words, font, max_width=300)
        assert len(rows) > 1
        # every word appears exactly once, in order
        assert [w for row in rows for w in row] == words

    def test_a_single_overlong_word_still_gets_its_own_row(self):
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
        rows = wrap_words(["supercalifragilisticexpialidocious"], font, max_width=10)
        assert rows == [["supercalifragilisticexpialidocious"]]


class TestLayoutWords:
    def test_preserves_word_count_and_order(self):
        words = ["Around", "500,000", "objects", "circle", "Earth"]
        layout = layout_words(words, FONT_PATH, FONT_SIZE, 940, LINE_HEIGHT)
        assert [item["text"] for item in layout] == words

    def test_rows_are_non_decreasing(self):
        words = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
        layout = layout_words(words, FONT_PATH, FONT_SIZE, 300, LINE_HEIGHT)
        rows = [item["row"] for item in layout]
        assert rows == sorted(rows)

    def test_each_row_fits_within_max_width(self):
        words = ["Around", "500,000", "objects", "circle", "Earth", "at", "over", "17,500", "miles"]
        max_width = 940
        layout = layout_words(words, FONT_PATH, FONT_SIZE, max_width, LINE_HEIGHT)

        by_row: dict[int, list[dict]] = {}
        for item in layout:
            by_row.setdefault(item["row"], []).append(item)

        for row_items in by_row.values():
            rightmost = max(item["x"] + item["width"] for item in row_items)
            assert rightmost <= max_width


class TestBlockHeight:
    def test_matches_row_count(self):
        words = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
        layout = layout_words(words, FONT_PATH, FONT_SIZE, 300, LINE_HEIGHT)
        num_rows = max(item["row"] for item in layout) + 1
        assert block_height(layout, LINE_HEIGHT) == num_rows * LINE_HEIGHT

    def test_empty_layout_is_zero(self):
        assert block_height([], LINE_HEIGHT) == 0


class TestRenderCaptionFrame:
    def test_canvas_size_matches_request(self):
        layout = layout_words(["Hello", "world"], FONT_PATH, FONT_SIZE, 940, LINE_HEIGHT)
        canvas = (940, LINE_HEIGHT)
        img = render_caption_frame(layout, 0, FONT_PATH, FONT_SIZE, canvas, (200, 200, 200), (255, 0, 0))
        assert img.size == canvas

    def test_active_word_is_drawn_in_highlight_color(self):
        layout = layout_words(["Hello", "world"], FONT_PATH, FONT_SIZE, 940, LINE_HEIGHT)
        canvas = (940, LINE_HEIGHT)
        base = (200, 200, 200)
        highlight = (255, 0, 0)

        img = render_caption_frame(layout, 1, FONT_PATH, FONT_SIZE, canvas, base, highlight)

        # sample a pixel inside the active word ("world", index 1) — some pixel in its
        # bounding box should be pure highlight color (glyph ink, not anti-aliased edge)
        active = layout[1]
        region = img.crop((int(active["x"]), 0, int(active["x"] + active["width"]), canvas[1]))
        colors = {px[:3] for px in region.getdata() if px[3] > 200}
        assert highlight in colors
        assert base not in colors

    def test_inactive_word_is_drawn_in_base_color(self):
        layout = layout_words(["Hello", "world"], FONT_PATH, FONT_SIZE, 940, LINE_HEIGHT)
        canvas = (940, LINE_HEIGHT)
        base = (200, 200, 200)
        highlight = (255, 0, 0)

        img = render_caption_frame(layout, 1, FONT_PATH, FONT_SIZE, canvas, base, highlight)

        inactive = layout[0]
        region = img.crop((int(inactive["x"]), 0, int(inactive["x"] + inactive["width"]), canvas[1]))
        colors = {px[:3] for px in region.getdata() if px[3] > 200}
        assert base in colors
        assert highlight not in colors
