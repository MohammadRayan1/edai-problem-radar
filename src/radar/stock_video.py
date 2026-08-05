from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
from anthropic import Anthropic

from radar.costs import record_anthropic_usage

PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
PEXELS_PHOTO_SEARCH_URL = "https://api.pexels.com/v1/search"
TARGET_FILE_HEIGHT = 1280  # near our render size without pulling huge 4K source files
CANDIDATES_PER_QUERY = 5  # per media type, per line — more candidates means more chances to
# clear the relevance check, at the cost of more thumbnail fetches/relevance rounds when the
# first choice doesn't hold up


def _coerce_tool_list(value: Any, key: str) -> list:
    """Claude's tool-use output occasionally double-encodes an array field as a raw JSON
    string instead of a native array — e.g. {"queries": "{\"queries\": [...]}"} instead of
    {"queries": [...]}. This is an intermittent tool-calling quirk (observed on ~half of
    calls in testing), not something any specific prompt triggers. Left unhandled, iterating
    that string per-character silently turns every query into a single letter, which then
    fails every search in that whole batch — a real, previously-unnoticed cause of lines
    coming up with no relevant match.
    """
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed[key] if isinstance(parsed, dict) else parsed
    return value


def _video_query_tool(num_lines: int) -> dict:
    return {
        "name": "emit_video_queries",
        "description": "Emit one short stock-footage search query per script line, in the same order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "minItems": num_lines,
                    "maxItems": num_lines,
                    "items": {
                        "type": "string",
                        "description": (
                            "A concrete, filmable scene or subject for stock-footage search — 2-4 words, "
                            "e.g. 'hospital nurse computer', 'factory assembly line', 'wheat field drought', "
                            "'cybersecurity code screen'. Describe something a camera could actually film that "
                            "matches this specific line's claim, not a generic or abstract stand-in — avoid "
                            "defaulting to generic 'office'/'business meeting' footage unless the line is "
                            "literally about an office or a meeting. For lines addressed directly to the "
                            "viewer with no literal scene of their own (calls to action like 'build the tool, "
                            "start now' or rhetorical hooks like 'what if this changed?'), query for a student "
                            "or young person actively coding, prototyping, or building something on a laptop — "
                            "this is footage for a program where teens go build a tech solution, so 'build' "
                            "means building software/tech, not literal construction."
                        ),
                    },
                }
            },
            "required": ["queries"],
        },
    }


def generate_video_queries(lines: list[str], client: Anthropic, model: str) -> list[str]:
    numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(lines))
    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=(
            "You pick one stock-footage search query per narration line for a short vertical explainer video "
            "made for a teen tech/entrepreneurship program (students research a real-world problem, then build "
            "a software tool to address it). Each query should describe a concrete, filmable real-world scene "
            "that matches what that specific line is actually claiming — not a generic mood shot. Keep the "
            "queries in the same order as the lines."
        ),
        tools=[_video_query_tool(len(lines))],
        tool_choice={"type": "tool", "name": "emit_video_queries"},
        messages=[{"role": "user", "content": f"Lines:\n{numbered}"}],
    )
    record_anthropic_usage("video_queries", model, message.usage.input_tokens, message.usage.output_tokens)

    tool_use = next(block for block in message.content if block.type == "tool_use")
    return _coerce_tool_list(tool_use.input["queries"], "queries")


def _slug_description(url: str) -> str:
    # Pexels embeds a human-written description in the video's URL slug, e.g.
    # https://www.pexels.com/video/aerial-view-of-industrial-complex-in-vietnam-31111118/
    # -> "aerial view of industrial complex in vietnam". This is real descriptive text
    # we can hand to Claude for a relevance judgment — Pexels doesn't return a separate
    # title/tags field on the video search response.
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    parts = slug.split("-")
    if parts and parts[-1].isdigit():
        parts = parts[:-1]
    return " ".join(parts)


def _best_video_file(video_files: list[dict]) -> str | None:
    candidates = [f for f in video_files if f.get("file_type") == "video/mp4" and f.get("height")]
    if not candidates:
        return None
    candidates.sort(key=lambda f: abs(f["height"] - TARGET_FILE_HEIGHT))
    return candidates[0]["link"]


def _best_photo_url(src: dict) -> str | None:
    # large2x is generous resolution without pulling the (often huge) original file — we
    # cover-crop to our own frame size in video_engine anyway, so we don't need more than this.
    for key in ("large2x", "large", "original"):
        if src.get(key):
            return src[key]
    return None


async def _search_json(
    client: httpx.AsyncClient, url: str, query: str, api_key: str, limit: int, result_key: str
) -> list[dict]:
    for params in (
        {"query": query, "per_page": limit, "orientation": "portrait"},
        {"query": query, "per_page": limit},  # fall back to any orientation if portrait finds nothing
    ):
        try:
            resp = await client.get(url, headers={"Authorization": api_key}, params=params)
            resp.raise_for_status()
            results = resp.json().get(result_key, [])
        except httpx.HTTPError:
            results = []
        if results:
            return results
    return []


async def _search_video_candidates(
    client: httpx.AsyncClient, query: str, api_key: str, limit: int = CANDIDATES_PER_QUERY
) -> list[dict]:
    videos = await _search_json(client, PEXELS_VIDEO_SEARCH_URL, query, api_key, limit, "videos")

    candidates = []
    for video in videos:
        file_url = _best_video_file(video.get("video_files", []))
        thumbnail_url = video.get("image")
        if file_url and thumbnail_url:
            candidates.append(
                {
                    "kind": "video",
                    "description": _slug_description(video.get("url", "")),
                    "file_url": file_url,
                    "thumbnail_url": thumbnail_url,
                }
            )
    return candidates


async def _search_photo_candidates(
    client: httpx.AsyncClient, query: str, api_key: str, limit: int = CANDIDATES_PER_QUERY
) -> list[dict]:
    photos = await _search_json(client, PEXELS_PHOTO_SEARCH_URL, query, api_key, limit, "photos")

    candidates = []
    for photo in photos:
        src = photo.get("src", {})
        file_url = _best_photo_url(src)
        thumbnail_url = src.get("tiny") or src.get("small")
        # Pexels' photo API returns a real human-written `alt` description directly — better
        # than the video API's URL-slug-derived one — but fall back to the slug if it's blank.
        description = (photo.get("alt") or "").strip() or _slug_description(photo.get("url", ""))
        if file_url and thumbnail_url and description:
            candidates.append(
                {"kind": "photo", "description": description, "file_url": file_url, "thumbnail_url": thumbnail_url}
            )
    return candidates


async def _gather_candidates(client: httpx.AsyncClient, query: str, api_key: str) -> list[dict]:
    # Video first (more engaging when it works), then photos — Pexels has real photo coverage
    # for far more subjects than it has usable video clips for, so this is where most of the
    # "found a genuine match" cases come from for niche topics.
    video_candidates = await _search_video_candidates(client, query, api_key)
    photo_candidates = await _search_photo_candidates(client, query, api_key)
    return video_candidates + photo_candidates


async def _fetch_thumbnail(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    return resp.content


def _relevance_tool(num_pairs: int) -> dict:
    return {
        "name": "emit_relevance_judgments",
        "description": "Judge whether each candidate stock clip is genuinely relevant to its line, in order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "judgments": {
                    "type": "array",
                    "minItems": num_pairs,
                    "maxItems": num_pairs,
                    "items": {
                        "type": "object",
                        "properties": {"relevant": {"type": "boolean"}},
                        "required": ["relevant"],
                    },
                }
            },
            "required": ["judgments"],
        },
    }


def check_relevance_batch(items: list[tuple[str, bytes]], client: Anthropic, model: str) -> list[bool]:
    """items: (line_text, thumbnail_jpeg_bytes). Returns one bool per item, same order.

    This is the step the earlier stock-footage attempt didn't have — that version trusted
    search results directly and shipped a video with an unrelated clip (business/COVID
    footage behind a space-industry line). A text-only check isn't enough either: a first
    pass here judged from Pexels' URL-slug description alone and still let through a
    stock-market-chart clip for a line about spacecraft engineers, and a "kid with a toy
    rocket in a park" clip for a real NASA mission line — the words matched ("data",
    "rocket") even though the actual footage didn't. So this looks at the real thumbnail
    image for each candidate, not just a text proxy for it.
    """
    if not items:
        return []

    content: list[dict] = []
    for i, (line_text, image_bytes) in enumerate(items):
        content.append({"type": "text", "text": f"Candidate {i}\nLINE: {line_text}\nCLIP THUMBNAIL:"})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(image_bytes).decode("ascii"),
                },
            }
        )

    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=(
            "You are the last check before a stock video clip is used as the background for a line of "
            "narration in a short explainer video. You'll see each candidate's actual thumbnail image next to "
            "the line it would play behind. Judge from what's really in the image, not just whether the "
            "search query shares a keyword with the line — reject anything that's visibly the wrong subject "
            "even if a word matched: a toy instead of the real thing, financial/stock-market charts instead of "
            "the actual technical or industrial subject, generic office footage, an unrelated news event, or "
            "any other case where the image's real content doesn't genuinely depict what the line describes. "
            "The clip doesn't need to be a perfect literal match — topically adjacent and visually sensible is "
            "fine — but if you can't see a real, honest connection between the image and the line, reject it.\n\n"
            "Some lines aren't factual claims at all — they're addressed straight to the viewer: rhetorical "
            "hooks ('What if this changed?'), hypotheticals ('Imagine a tool that...'), or calls to action "
            "('Build the tool. Start now.'). These have no single literal scene, so judge them on thematic fit "
            "instead: does the image show someone actively building, coding, or working on a tech solution (for "
            "'build/solve/start now' lines), or does it fit the general subject and mood of the video (for "
            "hooks/hypotheticals)? Approve a genuinely on-theme image for these even though it isn't a literal "
            "depiction of the sentence — only reject if the image's real subject is unrelated."
        ),
        tools=[_relevance_tool(len(items))],
        tool_choice={"type": "tool", "name": "emit_relevance_judgments"},
        messages=[{"role": "user", "content": content}],
    )
    record_anthropic_usage("stock_video_relevance", model, message.usage.input_tokens, message.usage.output_tokens)

    tool_use = next(block for block in message.content if block.type == "tool_use")
    judgments = _coerce_tool_list(tool_use.input["judgments"], "judgments")
    return [j["relevant"] for j in judgments]


async def _download_one(client: httpx.AsyncClient, url: str, dest_path: Path) -> bool:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError:
        return False
    dest_path.write_bytes(resp.content)
    return True


def _candidate_extension(kind: str) -> str:
    return ".mp4" if kind == "video" else ".jpg"


async def fetch_all_stock_visuals(
    line_texts: list[str],
    queries: list[str],
    api_key: str,
    anthropic_client: Anthropic,
    model: str,
    out_dir: Path,
) -> list[dict[str, Any] | None]:
    """Per line: search Pexels (video, then photo), verify relevance, download if approved.

    Each line gets up to CANDIDATES_PER_QUERY video candidates and CANDIDATES_PER_QUERY photo
    candidates, checked in that priority order. Checking happens in rounds across all lines at
    once (round 1 = each line's first candidate, round 2 = the second candidate but only for
    lines still unmatched after round 1, etc.) rather than per line, so a line with a great
    first candidate doesn't pay for a line that needs its fourth or fifth try — everyone's
    round-1 candidates get checked together in one batched relevance call, and only the
    stragglers carry into round 2.

    Returns one entry per line — {"type": "video"|"photo", "path": Path, "description": str}
    for an approved, downloaded candidate, or None if nothing relevant was found (caller falls
    back to an icon for that line).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any] | None] = [None] * len(queries)

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        per_line_candidates = list(await asyncio.gather(*(_gather_candidates(client, q, api_key) for q in queries)))

        max_rounds = max((len(c) for c in per_line_candidates), default=0)
        for round_idx in range(max_rounds):
            pending = [
                i for i in range(len(queries)) if results[i] is None and round_idx < len(per_line_candidates[i])
            ]
            if not pending:
                break

            thumbnails = await asyncio.gather(
                *(_fetch_thumbnail(client, per_line_candidates[i][round_idx]["thumbnail_url"]) for i in pending)
            )

            items: list[tuple[str, bytes]] = []
            item_indices: list[int] = []
            for i, thumb in zip(pending, thumbnails):
                if thumb is not None:
                    items.append((line_texts[i], thumb))
                    item_indices.append(i)

            relevance = check_relevance_batch(items, anthropic_client, model)
            approved = [item_indices[j] for j, ok in enumerate(relevance) if ok]

            async def download_and_store(i: int) -> None:
                candidate = per_line_candidates[i][round_idx]
                dest = out_dir / f"line_{i:03d}{_candidate_extension(candidate['kind'])}"
                ok = await _download_one(client, candidate["file_url"], dest)
                if ok:
                    results[i] = {"type": candidate["kind"], "path": dest, "description": candidate["description"]}

            await asyncio.gather(*(download_and_store(i) for i in approved))

    return results
