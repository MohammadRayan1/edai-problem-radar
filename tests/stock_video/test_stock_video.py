from __future__ import annotations

from radar.stock_video import TARGET_FILE_HEIGHT, _best_video_file, _slug_description, check_relevance_batch


class TestSlugDescription:
    def test_extracts_words_from_a_pexels_url_slug(self):
        url = "https://www.pexels.com/video/aerial-view-of-industrial-complex-in-vietnam-31111118/"

        assert _slug_description(url) == "aerial view of industrial complex in vietnam"

    def test_strips_the_trailing_numeric_id(self):
        url = "https://www.pexels.com/video/hospital-nurse-typing-computer-9876543/"

        result = _slug_description(url)

        assert "9876543" not in result
        assert result == "hospital nurse typing computer"

    def test_handles_a_url_without_a_trailing_slash(self):
        url = "https://www.pexels.com/video/wheat-field-drone-shot-12345"

        assert _slug_description(url) == "wheat field drone shot"

    def test_handles_a_slug_with_no_trailing_numeric_id(self):
        url = "https://www.pexels.com/video/wheat-field/"

        assert _slug_description(url) == "wheat field"


class TestBestVideoFile:
    def test_picks_the_file_closest_to_the_target_height(self):
        files = [
            {"file_type": "video/mp4", "height": 360, "link": "small.mp4"},
            {"file_type": "video/mp4", "height": 1280, "link": "target.mp4"},
            {"file_type": "video/mp4", "height": 2160, "link": "huge.mp4"},
        ]

        assert _best_video_file(files) == "target.mp4"

    def test_ignores_non_mp4_files(self):
        files = [
            {"file_type": "video/webm", "height": TARGET_FILE_HEIGHT, "link": "webm.webm"},
            {"file_type": "video/mp4", "height": 720, "link": "mp4.mp4"},
        ]

        assert _best_video_file(files) == "mp4.mp4"

    def test_returns_none_when_no_usable_file_exists(self):
        assert _best_video_file([]) is None
        assert _best_video_file([{"file_type": "video/webm", "height": 720, "link": "x.webm"}]) is None

    def test_ignores_files_missing_a_height(self):
        files = [{"file_type": "video/mp4", "link": "no-height.mp4"}, {"file_type": "video/mp4", "height": 900, "link": "ok.mp4"}]

        assert _best_video_file(files) == "ok.mp4"


class TestCheckRelevanceBatch:
    def test_returns_empty_list_for_no_pairs_without_calling_the_api(self):
        # None as the client would blow up if the function tried to use it — proves the
        # early-return path is taken instead of making a network call for zero candidates.
        assert check_relevance_batch([], client=None, model="claude-sonnet-5") == []
