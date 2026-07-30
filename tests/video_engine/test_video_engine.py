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
    _resolve_font,
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
