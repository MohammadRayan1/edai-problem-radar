from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx
from anthropic import Anthropic

from radar.costs import record_anthropic_usage

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
TARGET_FILE_HEIGHT = 1280  # near our render size without pulling huge 4K source files


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
                            "literally about an office or a meeting."
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
            "You pick one stock-footage search query per narration line for a short vertical explainer video. "
            "Each query should describe a concrete, filmable real-world scene that matches what that specific "
            "line is actually claiming — not a generic mood shot. Keep the queries in the same order as the "
            "lines."
        ),
        tools=[_video_query_tool(len(lines))],
        tool_choice={"type": "tool", "name": "emit_video_queries"},
        messages=[{"role": "user", "content": f"Lines:\n{numbered}"}],
    )
    record_anthropic_usage("video_queries", model, message.usage.input_tokens, message.usage.output_tokens)

    tool_use = next(block for block in message.content if block.type == "tool_use")
    return tool_use.input["queries"]


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


async def _search_one(client: httpx.AsyncClient, query: str, api_key: str) -> dict | None:
    for params in (
        {"query": query, "per_page": 3, "orientation": "portrait"},
        {"query": query, "per_page": 3},  # fall back to any orientation if portrait finds nothing
    ):
        try:
            resp = await client.get(PEXELS_SEARCH_URL, headers={"Authorization": api_key}, params=params)
            resp.raise_for_status()
            videos = resp.json().get("videos", [])
        except httpx.HTTPError:
            videos = []
        if videos:
            break
    else:
        return None

    video = videos[0]
    file_url = _best_video_file(video.get("video_files", []))
    thumbnail_url = video.get("image")
    if not file_url or not thumbnail_url:
        return None
    return {
        "description": _slug_description(video.get("url", "")),
        "file_url": file_url,
        "thumbnail_url": thumbnail_url,
    }


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
            "fine — but if you can't see a real, honest connection between the image and the line, reject it."
        ),
        tools=[_relevance_tool(len(items))],
        tool_choice={"type": "tool", "name": "emit_relevance_judgments"},
        messages=[{"role": "user", "content": content}],
    )
    record_anthropic_usage("stock_video_relevance", model, message.usage.input_tokens, message.usage.output_tokens)

    tool_use = next(block for block in message.content if block.type == "tool_use")
    return [j["relevant"] for j in tool_use.input["judgments"]]


async def _download_one(client: httpx.AsyncClient, url: str, dest_path: Path) -> bool:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError:
        return False
    dest_path.write_bytes(resp.content)
    return True


async def fetch_all_stock_visuals(
    line_texts: list[str],
    queries: list[str],
    api_key: str,
    anthropic_client: Anthropic,
    model: str,
    out_dir: Path,
) -> list[dict[str, Any] | None]:
    """Per line: search Pexels, verify relevance, download if approved.

    Returns one entry per line — {"type": "video", "path": Path, "description": str} for an
    approved, downloaded clip, or None if no relevant clip was found (caller falls back to an
    icon for that line).
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        candidates = list(await asyncio.gather(*(_search_one(client, q, api_key) for q in queries)))

        candidate_indices = [i for i, c in enumerate(candidates) if c is not None]
        thumbnails = await asyncio.gather(
            *(_fetch_thumbnail(client, candidates[i]["thumbnail_url"]) for i in candidate_indices)
        )

        items: list[tuple[str, bytes]] = []
        item_indices: list[int] = []
        for i, thumb in zip(candidate_indices, thumbnails):
            if thumb is not None:
                items.append((line_texts[i], thumb))
                item_indices.append(i)

        relevance = check_relevance_batch(items, anthropic_client, model)
        approved = {item_indices[j] for j, ok in enumerate(relevance) if ok}

        results: list[dict[str, Any] | None] = [None] * len(queries)

        async def download_and_store(i: int) -> None:
            dest = out_dir / f"line_{i:03d}.mp4"
            ok = await _download_one(client, candidates[i]["file_url"], dest)
            if ok:
                results[i] = {"type": "video", "path": dest, "description": candidates[i]["description"]}

        await asyncio.gather(*(download_and_store(i) for i in approved))

    return results
