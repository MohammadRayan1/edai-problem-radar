from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any

import cairosvg
import httpx
from anthropic import Anthropic
from PIL import Image, ImageDraw

from radar.costs import record_anthropic_usage

ICONIFY_SEARCH_URL = "https://api.iconify.design/search"
ICON_SIZE = 512
BADGE_SIZE = 640
FALLBACK_ICON = "material-symbols:lightbulb-outline"

# Cycled by line index so consecutive badges don't repeat the same color.
ACCENT_PALETTE: list[tuple[int, int, int]] = [
    (91, 141, 239),  # blue
    (247, 108, 108),  # coral
    (78, 205, 196),  # teal
    (255, 184, 77),  # amber
    (185, 131, 255),  # purple
    (107, 203, 119),  # green
]


def _icon_query_tool(num_lines: int) -> dict:
    return {
        "name": "emit_icon_queries",
        "description": "Emit one short icon search query per script line, in the same order.",
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
                            "A concrete, icon-searchable name for a physical object or symbol. Prefer a "
                            "disambiguating two-word compound over a bare noun whenever the bare noun could "
                            "collide with a common UI icon meaning — e.g. 'satellite dish' (not 'satellite', "
                            "which mostly matches a generic 'satellite map view' UI icon), 'warning triangle' "
                            "(not 'warning'), 'rocket launch' (not 'rocket'). Use a single word only when it's "
                            "already unambiguous, e.g. 'lightbulb', 'shield', 'radar'."
                        ),
                    },
                }
            },
            "required": ["queries"],
        },
    }


def generate_icon_queries(lines: list[str], client: Anthropic, model: str) -> list[str]:
    numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(lines))
    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=(
            "You pick one simple icon/pictogram concept per narration line for a motion-graphic explainer video. "
            "For each line, name a physical object or symbol that exists as a common flat icon. Icon search "
            "engines match compound icon names well (e.g. 'satellite dish', 'warning triangle') but fail on "
            "phrases nothing is actually named after (e.g. 'satellite orbit' returns nothing even though "
            "'satellite dish' works) — so pick a name you'd expect to literally be an icon's filename, not a "
            "descriptive phrase. Avoid abstract or emotional concepts — pick the most literal, icon-able noun "
            "in the line, and keep it on-topic for the line's actual subject (don't drift to generic "
            "'office'/'business' imagery). Keep the queries in the same order as the lines."
        ),
        tools=[_icon_query_tool(len(lines))],
        tool_choice={"type": "tool", "name": "emit_icon_queries"},
        messages=[{"role": "user", "content": f"Lines:\n{numbered}"}],
    )
    record_anthropic_usage("icon_queries", model, message.usage.input_tokens, message.usage.output_tokens)

    tool_use = next(block for block in message.content if block.type == "tool_use")
    return tool_use.input["queries"]


async def _search_icon_raw(client: httpx.AsyncClient, query: str) -> list[str]:
    try:
        resp = await client.get(ICONIFY_SEARCH_URL, params={"query": query, "limit": 5})
        resp.raise_for_status()
        return resp.json().get("icons", [])
    except httpx.HTTPError:
        return []


async def _search_icon(client: httpx.AsyncClient, query: str) -> str:
    # Iconify's search ANDs every word in the query — multi-word phrases like "satellite orbit"
    # routinely return zero results even when "satellite" alone has dozens of matches, so fall
    # back to just the first word before giving up to the generic fallback icon.
    icons = await _search_icon_raw(client, query)
    if not icons and " " in query:
        icons = await _search_icon_raw(client, query.split()[0])
    return icons[0] if icons else FALLBACK_ICON


def _render_badge(icon_svg_bytes: bytes, color: tuple[int, int, int], out_path: Path) -> None:
    icon_png_bytes = cairosvg.svg2png(bytestring=icon_svg_bytes, output_width=ICON_SIZE, output_height=ICON_SIZE)
    icon = Image.open(io.BytesIO(icon_png_bytes)).convert("RGBA")

    badge = Image.new("RGBA", (BADGE_SIZE, BADGE_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(badge)
    draw.ellipse([0, 0, BADGE_SIZE, BADGE_SIZE], fill=(*color, 255))

    offset = (BADGE_SIZE - ICON_SIZE) // 2
    badge.alpha_composite(icon, (offset, offset))

    badge.save(out_path)


async def _fetch_one(client: httpx.AsyncClient, query: str, index: int, out_dir: Path) -> dict[str, Any]:
    icon_id = await _search_icon(client, query)
    prefix, name = icon_id.split(":", 1)

    svg_bytes = None
    try:
        resp = await client.get(f"https://api.iconify.design/{prefix}/{name}.svg", params={"color": "#ffffff"})
        resp.raise_for_status()
        svg_bytes = resp.content
    except httpx.HTTPError:
        pass

    out_path = out_dir / f"line_{index:03d}.png"
    color = ACCENT_PALETTE[index % len(ACCENT_PALETTE)]

    if svg_bytes:
        await asyncio.to_thread(_render_badge, svg_bytes, color, out_path)
    else:
        # Last resort if even the fallback icon fetch failed: a plain colored badge, no icon.
        Image.new("RGBA", (BADGE_SIZE, BADGE_SIZE), (*color, 255)).save(out_path)

    return {"path": out_path, "icon_id": icon_id}


async def fetch_all_icons(queries: list[str], out_dir: Path) -> list[dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        return list(await asyncio.gather(*(_fetch_one(client, q, i, out_dir) for i, q in enumerate(queries))))
