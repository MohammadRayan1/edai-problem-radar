from __future__ import annotations

import asyncio
import base64
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import typer
from anthropic import Anthropic
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    concatenate_audioclips,
    vfx,
)
from rich.console import Console

from radar.captions import block_height, layout_words, render_caption_frame
from radar.config import Settings, get_settings
from radar.costs import record_tts_usage
from radar.models import Script
from radar.stock_video import fetch_all_stock_visuals, generate_video_queries
from radar.visuals import ACCENT_PALETTE, fetch_all_icons, generate_icon_queries

app = typer.Typer(add_completion=False)
console = Console()


class DurationExceededError(Exception):
    """Raised when the actual synthesized narration runs over the <60s product requirement.

    script_engine's pacing gate only estimates duration from word count, since real audio
    isn't available until after TTS. That estimate can undershoot the real ElevenLabs
    narration length, so this is the ground-truth backstop, checked before the (expensive)
    icon-sourcing and assembly steps run.
    """


VIDEO_SIZE = (1080, 1920)
FPS = 24
HARD_DURATION_CAP_SEC = 60.0

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
]

# Icon badge pop-in animation
BADGE_DISPLAY_SIZE = 560
BADGE_CENTER_Y = 620
POP_DURATION = 0.35
FRAME_CENTER_X = VIDEO_SIZE[0] / 2

# Karaoke caption layout. Each line's caption is rendered as one PIL image per word-state
# (which word is highlighted) rather than per-word MoviePy TextClips — compositing many
# differently-sized TextClips together was mis-sizing some short words (e.g. "are", "so").
CAPTION_FONT_SIZE = 52
CAPTION_MAX_WIDTH = 920
CAPTION_LINE_HEIGHT = 80
CAPTION_ZONE_TOP = 1100
CAPTION_ZONE_BOTTOM = 1780

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"


def _resolve_font(settings: Settings) -> str:
    if settings.font_path and Path(settings.font_path).exists():
        return settings.font_path
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("No usable font found. Set FONT_PATH in .env to a .ttf/.ttc file.")


def _load_script(path: Path) -> Script:
    return Script(**json.loads(path.read_text()))


def _flatten_lines(script: Script) -> list[dict]:
    lines = []
    for section in script.sections:
        for line in section.lines:
            lines.append(
                {
                    "section": section.name,
                    "text": line.text,
                    "citation_indices": line.citation_indices,
                }
            )
    return lines


def _words_from_alignment(alignment: dict) -> list[dict]:
    """Group ElevenLabs' character-level timestamps into words by whitespace.

    The `characters` array is the input text verbatim, position-for-position, so this
    is an exact reconstruction — no separate tokenizer to disagree with our own text.
    """
    characters = alignment["characters"]
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]

    words: list[dict] = []
    current_start: float | None = None
    current_chars: list[str] = []
    for i, ch in enumerate(characters):
        if ch.isspace():
            if current_chars:
                words.append({"text": "".join(current_chars), "start": current_start, "end": ends[i - 1]})
                current_chars = []
                current_start = None
        else:
            if current_start is None:
                current_start = starts[i]
            current_chars.append(ch)
    if current_chars:
        words.append({"text": "".join(current_chars), "start": current_start, "end": ends[-1]})

    return words


TTS_MAX_CONCURRENCY = 3  # ElevenLabs plans cap concurrent requests; stay comfortably under it
TTS_MAX_RETRIES = 4
TTS_RETRY_BASE_DELAY = 2.0  # seconds, doubles each retry


async def _synthesize_one(
    client: httpx.AsyncClient, semaphore: asyncio.Semaphore, voice_id: str, api_key: str, model_id: str, text: str
) -> dict:
    async with semaphore:
        for attempt in range(TTS_MAX_RETRIES):
            resp = await client.post(
                ELEVENLABS_TTS_URL.format(voice_id=voice_id),
                headers={"xi-api-key": api_key},
                json={"text": text, "model_id": model_id},
            )
            if resp.status_code == 429 and attempt < TTS_MAX_RETRIES - 1:
                await asyncio.sleep(TTS_RETRY_BASE_DELAY * (2**attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()  # last attempt's failure, if we fell through
        return resp.json()


async def _synthesize_all(
    lines: list[dict], voice_id: str, api_key: str, model_id: str, audio_dir: Path
) -> tuple[list[Path], list[list[dict]]]:
    audio_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(TTS_MAX_CONCURRENCY)

    async def synth_one(client: httpx.AsyncClient, i: int, text: str) -> tuple[Path, list[dict]]:
        data = await _synthesize_one(client, semaphore, voice_id, api_key, model_id, text)

        out_path = audio_dir / f"line_{i:03d}.mp3"
        out_path.write_bytes(base64.b64decode(data["audio_base64"]))

        record_tts_usage("narration", len(text))

        return out_path, _words_from_alignment(data["alignment"])

    async with httpx.AsyncClient(timeout=60.0) as client:
        results = list(await asyncio.gather(*(synth_one(client, i, l["text"]) for i, l in enumerate(lines))))
    return [r[0] for r in results], [r[1] for r in results]


def _build_timeline(
    lines: list[dict], audio_paths: list[Path], word_timings: list[list[dict]]
) -> tuple[list[dict], list[AudioFileClip]]:
    clips = [AudioFileClip(str(p)) for p in audio_paths]
    timeline = []
    t = 0.0
    for line, clip, words in zip(lines, clips, word_timings):
        start, end = t, t + clip.duration
        absolute_words = [{"text": w["text"], "start": start + w["start"], "end": start + w["end"]} for w in words]
        timeline.append({**line, "start": start, "end": end, "words": absolute_words})
        t = end
    return timeline, clips


def _check_duration_cap(duration_sec: float) -> None:
    if duration_sec > HARD_DURATION_CAP_SEC:
        raise DurationExceededError(
            f"Actual narration duration {duration_sec:.1f}s exceeds the "
            f"{HARD_DURATION_CAP_SEC:.0f}s hard product requirement — script_engine's pacing "
            "gate estimate undershot the real TTS output for this script. Regenerate the script."
        )


def _ease_out_back(p: float) -> float:
    c1 = 1.70158
    c3 = c1 + 1
    p -= 1
    return 1 + c3 * p**3 + c1 * p**2


def _badge_scale(t: float, duration: float) -> float:
    if t < POP_DURATION:
        return max(0.05, _ease_out_back(min(t, POP_DURATION) / POP_DURATION))
    settle_t = t - POP_DURATION
    return 1.0 + 0.025 * math.sin(2 * math.pi * settle_t / 2.2)


def _badge_position(t: float, duration: float) -> tuple[float, float]:
    size = BADGE_DISPLAY_SIZE * _badge_scale(t, duration)
    return (FRAME_CENTER_X - size / 2, BADGE_CENTER_Y - size / 2)


def _prepare_stock_clip(path: Path, duration: float) -> VideoFileClip:
    """Loop or trim a stock clip to exactly `duration`, then cover-crop it to VIDEO_SIZE.

    Stock footage is rarely native 9:16, so this scales up by whichever axis needs it
    more and center-crops the overflow — the same "cover" behavior as CSS
    background-size: cover, just done by hand since MoviePy doesn't have that built in.
    """
    clip = VideoFileClip(str(path)).without_audio()
    clip = clip.with_effects([vfx.Loop(duration=duration)]) if clip.duration < duration else clip.subclipped(0, duration)

    scale = max(VIDEO_SIZE[0] / clip.w, VIDEO_SIZE[1] / clip.h)
    clip = clip.resized(scale)
    clip = clip.cropped(x_center=clip.w / 2, y_center=clip.h / 2, width=VIDEO_SIZE[0], height=VIDEO_SIZE[1])
    return clip


def _build_background_clip(visual: dict | None, start: float, duration: float):
    dark_bg = ColorClip(size=VIDEO_SIZE, color=(15, 15, 20), duration=duration)

    if visual is None:
        return dark_bg.with_start(start)

    if visual["type"] == "video":
        clip = _prepare_stock_clip(visual["path"], duration)
        return clip.with_start(start)

    badge = ImageClip(str(visual["path"])).resized(width=BADGE_DISPLAY_SIZE)
    animated_badge = (
        badge.resized(lambda t: _badge_scale(t, duration))
        .with_position(lambda t: _badge_position(t, duration))
        .with_duration(duration)
    )

    composite = CompositeVideoClip([dark_bg, animated_badge], size=VIDEO_SIZE)
    return composite.with_start(start)


CAPTION_BASE_RGB = (232, 232, 232)


def _build_caption_clips(item: dict, font: str, highlight_color: tuple[int, int, int]) -> list:
    words = item["words"] or [{"text": item["text"], "start": item["start"], "end": item["end"]}]
    word_texts = [w["text"] for w in words]

    layout = layout_words(word_texts, font, CAPTION_FONT_SIZE, CAPTION_MAX_WIDTH, CAPTION_LINE_HEIGHT)
    height = block_height(layout, CAPTION_LINE_HEIGHT)

    zone_height = CAPTION_ZONE_BOTTOM - CAPTION_ZONE_TOP
    block_top = CAPTION_ZONE_TOP + max(0, (zone_height - height) / 2)
    box_left = (VIDEO_SIZE[0] - CAPTION_MAX_WIDTH) / 2
    line_start, line_end = item["start"], item["end"]

    # Each word stays highlighted until the next one starts (covers inter-word gaps, no flicker).
    windows = []
    for i, word in enumerate(words):
        start = word["start"] if i > 0 else line_start
        end = words[i + 1]["start"] if i + 1 < len(words) else line_end
        windows.append((start, max(end, start + 0.05)))

    clips = []
    for i, (start, end) in enumerate(windows):
        frame = render_caption_frame(
            layout, i, font, CAPTION_FONT_SIZE, (CAPTION_MAX_WIDTH, height), CAPTION_BASE_RGB, highlight_color
        )
        clip = (
            ImageClip(np.array(frame))
            .with_position((box_left, block_top))
            .with_start(start)
            .with_duration(end - start)
        )
        clips.append(clip)

    return clips


def _assemble_video(
    timeline: list[dict], clips: list[AudioFileClip], visuals: list[dict | None], font: str
) -> CompositeVideoClip:
    backgrounds = [
        _build_background_clip(visual, item["start"], item["end"] - item["start"])
        for item, visual in zip(timeline, visuals)
    ]

    overlays = []
    for i, item in enumerate(timeline):
        highlight_color = ACCENT_PALETTE[i % len(ACCENT_PALETTE)]
        overlays.extend(_build_caption_clips(item, font, highlight_color))

    audio = concatenate_audioclips(clips)

    video = CompositeVideoClip([*backgrounds, *overlays], size=VIDEO_SIZE)
    return video.with_audio(audio)


def _attach_visual_metadata(timeline: list[dict], visuals: list[dict | None]) -> None:
    for i, item in enumerate(timeline):
        v = visuals[i]
        item["visual_type"] = v["type"] if v else None
        item["visual_query"] = v.get("query") if v else None
        item["icon_id"] = v.get("icon_id") if v else None


def _save_meta(
    script: Script, script_path: Path, timeline: list[dict], video_path: Path, voice: str, meta_path: Path
) -> None:
    meta = {
        "problem_title": script.problem_title,
        "domain": script.domain,
        "script_path": str(script_path),
        "research_source_path": script.source_path,
        "video_path": str(video_path),
        "voice": voice,
        "total_duration_seconds": timeline[-1]["end"],
        "lines": timeline,
    }
    meta_path.write_text(json.dumps(meta, indent=2))


def generate_video(
    script: Script, script_path: Path, settings: Settings, output_dir: Path, voice: str | None = None
) -> Path:
    """Assemble a vertical 9:16 draft video with TTS narration, karaoke captions, and icon visuals."""
    selected_voice = voice or settings.elevenlabs_voice_id
    font = _resolve_font(settings)

    slug = script.problem_title.lower().replace(" ", "_").replace("/", "_")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    draft_dir = output_dir / f"{slug}_{timestamp}"
    draft_dir.mkdir(parents=True, exist_ok=True)

    lines = _flatten_lines(script)

    console.print(f"[bold]Synthesizing narration[/bold] ({len(lines)} lines, voice={selected_voice})...")
    audio_paths, word_timings = asyncio.run(
        _synthesize_all(lines, selected_voice, settings.elevenlabs_api_key, settings.elevenlabs_model_id, draft_dir / "audio")
    )

    timeline, clips = _build_timeline(lines, audio_paths, word_timings)
    console.print(f"Narration duration: {timeline[-1]['end']:.1f}s")

    try:
        _check_duration_cap(timeline[-1]["end"])
    except DurationExceededError:
        for clip in clips:
            clip.close()
        raise

    anthropic_client = Anthropic(api_key=settings.anthropic_api_key)
    line_texts = [l["text"] for l in lines]

    visuals: list[dict | None] = [None] * len(lines)
    if settings.pexels_api_key:
        console.print("[bold]Sourcing stock video visuals...[/bold]")
        video_queries = generate_video_queries(line_texts, anthropic_client, settings.anthropic_model)
        video_visuals = asyncio.run(
            fetch_all_stock_visuals(
                line_texts,
                video_queries,
                settings.pexels_api_key,
                anthropic_client,
                settings.anthropic_model,
                draft_dir / "stock_video",
            )
        )
        for i, v in enumerate(video_visuals):
            if v:
                v["query"] = video_queries[i]
                visuals[i] = v
        found = sum(1 for v in visuals if v)
        console.print(f"Found relevant stock video for {found}/{len(lines)} lines.")

    fallback_indices = [i for i, v in enumerate(visuals) if v is None]
    if fallback_indices:
        console.print(f"[bold]Sourcing icon visuals for the remaining {len(fallback_indices)} line(s)...[/bold]")
        fallback_texts = [line_texts[i] for i in fallback_indices]
        icon_queries = generate_icon_queries(fallback_texts, anthropic_client, settings.anthropic_model)
        icon_visuals = asyncio.run(fetch_all_icons(icon_queries, draft_dir / "visuals"))
        for idx, query, icon_visual in zip(fallback_indices, icon_queries, icon_visuals):
            visuals[idx] = {
                "type": "icon",
                "path": icon_visual["path"],
                "icon_id": icon_visual["icon_id"],
                "query": query,
            }

    _attach_visual_metadata(timeline, visuals)

    console.print("[bold]Assembling video...[/bold]")
    video = _assemble_video(timeline, clips, visuals, font)

    video_path = draft_dir / "draft.mp4"
    video.write_videofile(str(video_path), fps=FPS, codec="libx264", audio_codec="aac", logger=None)

    for clip in clips:
        clip.close()
    video.close()

    meta_path = draft_dir / "meta.json"
    _save_meta(script, script_path, timeline, video_path, selected_voice, meta_path)

    console.print(f"[bold green]Saved draft:[/bold green] {video_path}")
    console.print(f"[bold]Metadata:[/bold] {meta_path}")
    return video_path


@app.command()
def run(
    script_path: Path = typer.Argument(..., help="Path to a script_engine output JSON in data/scripts/"),
    output_dir: Path = typer.Option(Path("data/drafts"), help="Where to save the draft video"),
    voice: str | None = typer.Option(None, help="ElevenLabs voice_id to use (overrides config default)"),
) -> None:
    """Assemble a vertical 9:16 draft video with TTS narration, karaoke captions, and icon visuals."""
    settings = get_settings()
    script = _load_script(script_path)
    console.print(f"[bold]Loaded script:[/bold] {script.problem_title}")
    generate_video(script, script_path, settings, output_dir, voice)


if __name__ == "__main__":
    app()
