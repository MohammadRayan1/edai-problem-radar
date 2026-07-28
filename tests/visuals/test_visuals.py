from __future__ import annotations

import json

import httpx
import pytest
from PIL import Image

from radar.visuals import BADGE_SIZE, FALLBACK_ICON, _fetch_one, _render_badge, _search_icon

# A minimal valid SVG — a red square — enough for cairosvg to rasterize.
SIMPLE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" fill="red"/></svg>'


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestSearchIcon:
    async def test_falls_back_to_first_word_when_full_phrase_returns_nothing(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            query = httpx.QueryParams(request.url.query.decode())["query"]
            calls.append(query)
            if query == "satellite orbit":
                return httpx.Response(200, json={"icons": []})
            if query == "satellite":
                return httpx.Response(200, json={"icons": ["mdi:satellite-variant"]})
            return httpx.Response(200, json={"icons": []})

        async with make_client(handler) as client:
            result = await _search_icon(client, "satellite orbit")

        assert result == "mdi:satellite-variant"
        assert calls == ["satellite orbit", "satellite"]

    async def test_returns_first_result_when_phrase_matches_directly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"icons": ["boxicons:satellite-dish", "mdi:satellite-variant"]})

        async with make_client(handler) as client:
            result = await _search_icon(client, "satellite dish")

        assert result == "boxicons:satellite-dish"

    async def test_returns_fallback_icon_when_nothing_matches_at_all(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"icons": []})

        async with make_client(handler) as client:
            result = await _search_icon(client, "completely nonexistent gibberish query")

        assert result == FALLBACK_ICON

    async def test_single_word_query_only_makes_one_request(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json={"icons": []})

        async with make_client(handler) as client:
            await _search_icon(client, "satellite")

        assert len(calls) == 1


class TestRenderBadge:
    def test_produces_a_badge_sized_image(self, tmp_path):
        out_path = tmp_path / "badge.png"
        _render_badge(SIMPLE_SVG, (91, 141, 239), out_path)

        img = Image.open(out_path)
        assert img.size == (BADGE_SIZE, BADGE_SIZE)

    def test_corners_are_transparent_and_center_is_not(self, tmp_path):
        out_path = tmp_path / "badge.png"
        _render_badge(SIMPLE_SVG, (91, 141, 239), out_path)

        img = Image.open(out_path).convert("RGBA")
        corner = img.getpixel((2, 2))
        center = img.getpixel((BADGE_SIZE // 2, BADGE_SIZE // 2))

        assert corner[3] == 0  # fully transparent outside the circle
        assert center[3] == 255  # fully opaque inside the badge


class TestFetchOne:
    async def test_writes_a_badge_file_and_returns_metadata(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            if "search" in request.url.path:
                return httpx.Response(200, json={"icons": ["mdi:satellite-variant"]})
            return httpx.Response(200, content=SIMPLE_SVG)

        async with make_client(handler) as client:
            result = await _fetch_one(client, "satellite", 0, tmp_path)

        assert result["icon_id"] == "mdi:satellite-variant"
        assert result["path"].exists()
        assert Image.open(result["path"]).size == (BADGE_SIZE, BADGE_SIZE)

    async def test_falls_back_to_plain_color_badge_when_svg_fetch_fails(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            if "search" in request.url.path:
                return httpx.Response(200, json={"icons": ["mdi:satellite-variant"]})
            return httpx.Response(500)

        async with make_client(handler) as client:
            result = await _fetch_one(client, "satellite", 0, tmp_path)

        assert result["path"].exists()
        img = Image.open(result["path"])
        assert img.size == (BADGE_SIZE, BADGE_SIZE)
