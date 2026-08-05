from __future__ import annotations

import httpx

from radar.stock_video import (
    CANDIDATES_PER_QUERY,
    TARGET_FILE_HEIGHT,
    _best_photo_url,
    _best_video_file,
    _candidate_extension,
    _gather_candidates,
    _search_photo_candidates,
    _search_video_candidates,
    _slug_description,
    check_relevance_batch,
)


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


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


class TestBestPhotoUrl:
    def test_prefers_large2x(self):
        src = {"large2x": "big.jpg", "large": "medium.jpg", "original": "huge.jpg"}

        assert _best_photo_url(src) == "big.jpg"

    def test_falls_back_to_large_then_original(self):
        assert _best_photo_url({"large": "medium.jpg", "original": "huge.jpg"}) == "medium.jpg"
        assert _best_photo_url({"original": "huge.jpg"}) == "huge.jpg"

    def test_returns_none_when_nothing_usable(self):
        assert _best_photo_url({}) is None
        assert _best_photo_url({"tiny": "t.jpg", "small": "s.jpg"}) is None


class TestCandidateExtension:
    def test_video_gets_mp4(self):
        assert _candidate_extension("video") == ".mp4"

    def test_photo_gets_jpg(self):
        assert _candidate_extension("photo") == ".jpg"


class TestSearchVideoCandidates:
    async def test_returns_a_candidate_per_result_with_a_usable_file_and_thumbnail(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "videos": [
                        {
                            "url": "https://www.pexels.com/video/factory-workers-on-assembly-line-123/",
                            "image": "https://images.pexels.com/videos/123/thumb.jpeg",
                            "video_files": [{"file_type": "video/mp4", "height": 1280, "link": "vid.mp4"}],
                        }
                    ]
                },
            )

        async with make_client(handler) as client:
            candidates = await _search_video_candidates(client, "factory workers", "fake-key")

        assert len(candidates) == 1
        assert candidates[0]["kind"] == "video"
        assert candidates[0]["description"] == "factory workers on assembly line"
        assert candidates[0]["file_url"] == "vid.mp4"

    async def test_skips_results_missing_a_usable_video_file(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"videos": [{"url": "https://www.pexels.com/video/x-1/", "image": "t.jpeg", "video_files": []}]},
            )

        async with make_client(handler) as client:
            candidates = await _search_video_candidates(client, "query", "fake-key")

        assert candidates == []

    async def test_returns_empty_list_when_search_finds_nothing(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"videos": []})

        async with make_client(handler) as client:
            candidates = await _search_video_candidates(client, "query", "fake-key")

        assert candidates == []


class TestSearchPhotoCandidates:
    async def test_uses_the_real_alt_text_as_description(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "photos": [
                        {
                            "url": "https://www.pexels.com/photo/some-slug-999/",
                            "alt": "Two nurses in scrubs discussing patient charts",
                            "src": {"large2x": "big.jpg", "tiny": "thumb.jpg"},
                        }
                    ]
                },
            )

        async with make_client(handler) as client:
            candidates = await _search_photo_candidates(client, "hospital nurse", "fake-key")

        assert len(candidates) == 1
        assert candidates[0]["kind"] == "photo"
        assert candidates[0]["description"] == "Two nurses in scrubs discussing patient charts"
        assert candidates[0]["file_url"] == "big.jpg"
        assert candidates[0]["thumbnail_url"] == "thumb.jpg"

    async def test_falls_back_to_slug_description_when_alt_is_blank(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "photos": [
                        {
                            "url": "https://www.pexels.com/photo/wheat-field-drought-456/",
                            "alt": "",
                            "src": {"large2x": "big.jpg", "tiny": "thumb.jpg"},
                        }
                    ]
                },
            )

        async with make_client(handler) as client:
            candidates = await _search_photo_candidates(client, "wheat field", "fake-key")

        assert candidates[0]["description"] == "wheat field drought"

    async def test_skips_photos_missing_a_usable_image_url(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"photos": [{"url": "https://www.pexels.com/photo/x-1/", "alt": "x", "src": {}}]}
            )

        async with make_client(handler) as client:
            candidates = await _search_photo_candidates(client, "query", "fake-key")

        assert candidates == []


class TestGatherCandidates:
    async def test_puts_video_candidates_before_photo_candidates(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/videos/" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "videos": [
                            {
                                "url": "https://www.pexels.com/video/a-1/",
                                "image": "t.jpeg",
                                "video_files": [{"file_type": "video/mp4", "height": 1280, "link": "vid.mp4"}],
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={"photos": [{"url": "https://www.pexels.com/photo/b-2/", "alt": "a photo", "src": {"large2x": "photo.jpg", "tiny": "pt.jpg"}}]},
            )

        async with make_client(handler) as client:
            candidates = await _gather_candidates(client, "query", "fake-key")

        assert [c["kind"] for c in candidates] == ["video", "photo"]

    async def test_returns_only_photos_when_video_search_finds_nothing(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/videos/" in str(request.url):
                return httpx.Response(200, json={"videos": []})
            return httpx.Response(
                200,
                json={"photos": [{"url": "https://www.pexels.com/photo/b-2/", "alt": "a photo", "src": {"large2x": "photo.jpg", "tiny": "pt.jpg"}}]},
            )

        async with make_client(handler) as client:
            candidates = await _gather_candidates(client, "query", "fake-key")

        assert [c["kind"] for c in candidates] == ["photo"]
