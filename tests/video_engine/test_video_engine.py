from __future__ import annotations

import pytest

from radar.config import Settings
from radar.models import Script, ScriptLine, ScriptSection
from radar.video_engine import (
    BADGE_DISPLAY_SIZE,
    DURATION_GRACE_SEC,
    FRAME_CENTER_X,
    HARD_DURATION_CAP_SEC,
    POP_DURATION,
    DurationExceededError,
    _attach_visual_metadata,
    _badge_position,
    _badge_scale,
    _check_duration_cap,
    _ease_out_back,
    _flatten_lines,
    _photo_position,
    _photo_scale,
    _resolve_font,
    _use_real_imagery_for_all_lines,
)


class TestCheckDurationCap:
    def test_passes_silently_when_under_the_cap(self):
        _check_duration_cap(HARD_DURATION_CAP_SEC - 1)

    def test_passes_silently_exactly_at_the_cap(self):
        _check_duration_cap(HARD_DURATION_CAP_SEC)

    def test_passes_silently_within_the_grace_period(self):
        _check_duration_cap(HARD_DURATION_CAP_SEC + 0.7)
        _check_duration_cap(HARD_DURATION_CAP_SEC + DURATION_GRACE_SEC)

    def test_raises_when_actual_narration_exceeds_the_cap_plus_grace(self):
        with pytest.raises(DurationExceededError):
            _check_duration_cap(HARD_DURATION_CAP_SEC + DURATION_GRACE_SEC + 0.1)


class TestEaseOutBack:
    def test_starts_at_zero(self):
        assert _ease_out_back(0) == pytest.approx(0, abs=1e-6)

    def test_ends_at_one(self):
        assert _ease_out_back(1) == pytest.approx(1, abs=1e-6)


class TestBadgeScale:
    def test_settles_near_one_after_the_pop_finishes(self):
        # after the pop window, the badge should hover close to full size (idle pulse is small)
        scale = _badge_scale(POP_DURATION + 1.0, duration=10)
        assert 0.9 < scale < 1.1

    def test_starts_small(self):
        scale = _badge_scale(0.0, duration=10)
        assert scale < 0.5


class TestBadgePosition:
    def test_badge_stays_horizontally_centered_at_any_scale(self):
        for t in [0.0, 0.1, POP_DURATION, POP_DURATION + 2.0]:
            x, _ = _badge_position(t, duration=10)
            size = BADGE_DISPLAY_SIZE * _badge_scale(t, duration=10)
            assert x + size / 2 == pytest.approx(FRAME_CENTER_X)


class TestFlattenLines:
    def test_preserves_section_names_text_and_order(self):
        script = Script(
            problem_title="Test",
            domain="Testing",
            source_path="fake.json",
            sections=[
                ScriptSection(
                    name="Hook", start_sec=0, end_sec=4, lines=[ScriptLine(text="First line", citation_indices=[])]
                ),
                ScriptSection(
                    name="Opportunity",
                    start_sec=4,
                    end_sec=10,
                    lines=[
                        ScriptLine(text="Second line", citation_indices=[0]),
                        ScriptLine(text="Third line", citation_indices=[]),
                    ],
                ),
            ],
            evidence_ledger=[],
        )

        lines = _flatten_lines(script)

        assert [l["text"] for l in lines] == ["First line", "Second line", "Third line"]
        assert [l["section"] for l in lines] == ["Hook", "Opportunity", "Opportunity"]
        assert lines[1]["citation_indices"] == [0]


class TestUseRealImageryForAllLines:
    def test_true_when_every_line_has_a_clip(self):
        real_visuals = [{"type": "video", "path": "a.mp4"}, {"type": "video", "path": "b.mp4"}]

        assert _use_real_imagery_for_all_lines(real_visuals) is True

    def test_true_when_video_and_photo_are_mixed(self):
        # mixing video and photo is fine — both are "real imagery"; only mixing with icons isn't.
        real_visuals = [{"type": "video", "path": "a.mp4"}, {"type": "photo", "path": "b.jpg"}]

        assert _use_real_imagery_for_all_lines(real_visuals) is True

    def test_false_when_one_line_is_missing_a_match(self):
        real_visuals = [{"type": "video", "path": "a.mp4"}, None, {"type": "photo", "path": "c.jpg"}]

        assert _use_real_imagery_for_all_lines(real_visuals) is False

    def test_false_when_no_lines_have_a_match(self):
        assert _use_real_imagery_for_all_lines([None, None]) is False

    def test_false_for_an_empty_list(self):
        assert _use_real_imagery_for_all_lines([]) is False


class TestPhotoKenBurns:
    def test_scale_grows_from_base_toward_base_times_zoom(self):
        base_scale = 2.0
        start = _photo_scale(0.0, 10.0, base_scale)
        end = _photo_scale(10.0, 10.0, base_scale)

        assert start == pytest.approx(base_scale)
        assert end > start

    def test_scale_never_exceeds_the_end_of_the_zoom_range(self):
        base_scale = 1.5
        past_end = _photo_scale(999.0, 10.0, base_scale)
        at_end = _photo_scale(10.0, 10.0, base_scale)

        assert past_end == pytest.approx(at_end)

    def test_position_keeps_the_image_centered_as_it_scales(self):
        # the image's on-screen center should stay fixed at the frame's center throughout
        # the zoom, even though its pixel size grows — otherwise it visibly drifts.
        from radar.video_engine import VIDEO_SIZE

        img_w, img_h = 1000, 1500
        base_scale = 1.2
        for t in (0.0, 2.5, 5.0):
            x, y = _photo_position(t, 5.0, base_scale, img_w, img_h)
            scale = _photo_scale(t, 5.0, base_scale)
            center_x = x + (img_w * scale) / 2
            center_y = y + (img_h * scale) / 2
            assert center_x == pytest.approx(VIDEO_SIZE[0] / 2)
            assert center_y == pytest.approx(VIDEO_SIZE[1] / 2)


class TestAttachVisualMetadata:
    def test_attaches_query_and_icon_id_for_an_icon_visual(self):
        timeline = [{"text": "a"}, {"text": "b"}]
        visuals = [
            {"type": "icon", "icon_id": "mdi:satellite-variant", "path": "x.png", "query": "satellite"},
            None,
        ]

        _attach_visual_metadata(timeline, visuals)

        assert timeline[0]["visual_type"] == "icon"
        assert timeline[0]["visual_query"] == "satellite"
        assert timeline[0]["icon_id"] == "mdi:satellite-variant"
        assert timeline[1]["visual_type"] is None
        assert timeline[1]["visual_query"] is None
        assert timeline[1]["icon_id"] is None

    def test_attaches_query_for_a_video_visual_with_no_icon_id(self):
        timeline = [{"text": "a"}]
        visuals = [{"type": "video", "path": "x.mp4", "query": "hospital nurse", "description": "hospital nurse"}]

        _attach_visual_metadata(timeline, visuals)

        assert timeline[0]["visual_type"] == "video"
        assert timeline[0]["visual_query"] == "hospital nurse"
        assert timeline[0]["icon_id"] is None


class TestResolveFont:
    def test_prefers_explicit_setting_when_it_exists(self, tmp_path):
        font_file = tmp_path / "custom.ttf"
        font_file.write_text("not a real font, just needs to exist")

        settings = Settings(
            tavily_api_key="x", anthropic_api_key="x", font_path=str(font_file)
        )

        assert _resolve_font(settings) == str(font_file)

    def test_raises_when_nothing_is_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr("radar.video_engine.FONT_CANDIDATES", [str(tmp_path / "nonexistent.ttf")])
        settings = Settings(tavily_api_key="x", anthropic_api_key="x", font_path=None)

        with pytest.raises(RuntimeError):
            _resolve_font(settings)
