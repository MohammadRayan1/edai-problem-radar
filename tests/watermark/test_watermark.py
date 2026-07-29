from __future__ import annotations

import pytest

from radar.watermark import FRAME_BOTTOM_MARGIN, SAFE_ZONE_TOP, VIDEO_SIZE, _compute_position


class TestComputePosition:
    def test_centers_horizontally(self):
        x, y = _compute_position(300, 50)

        assert x == (VIDEO_SIZE[0] - 300) / 2

    def test_stays_below_the_caption_safe_zone(self):
        _, y = _compute_position(300, 50)

        assert y >= SAFE_ZONE_TOP

    def test_never_crosses_the_bottom_edge_of_the_frame(self):
        # a watermark tall enough to fill almost the entire safe band
        wm_h = VIDEO_SIZE[1] - SAFE_ZONE_TOP - FRAME_BOTTOM_MARGIN - 1
        _, y = _compute_position(300, wm_h)

        assert y + wm_h <= VIDEO_SIZE[1] - FRAME_BOTTOM_MARGIN

    def test_raises_if_the_watermark_is_too_wide_for_the_frame(self):
        with pytest.raises(RuntimeError):
            _compute_position(VIDEO_SIZE[0] + 1, 50)

    def test_raises_if_the_watermark_is_too_tall_for_the_safe_zone(self):
        too_tall = (VIDEO_SIZE[1] - SAFE_ZONE_TOP) + 1
        with pytest.raises(RuntimeError):
            _compute_position(300, too_tall)

    def test_a_normal_sized_watermark_fits_entirely_within_the_frame(self):
        wm_w, wm_h = 220, 45
        x, y = _compute_position(wm_w, wm_h)

        assert x >= 0
        assert x + wm_w <= VIDEO_SIZE[0]
        assert y >= 0
        assert y + wm_h <= VIDEO_SIZE[1]
