from __future__ import annotations

from pathlib import Path

import numpy as np
import typer
from moviepy import CompositeVideoClip, ImageClip, VideoFileClip
from PIL import Image, ImageDraw, ImageFont
from rich.console import Console

from radar.config import get_settings
from radar.video_engine import VIDEO_SIZE, _resolve_font

app = typer.Typer(add_completion=False)
console = Console()

WATERMARK_TEXT = "EdAI Inc"
WATERMARK_FONT_SIZE = 34
WATERMARK_COLOR = (255, 255, 255, 210)
WATERMARK_PADDING = 20

# Captions never render below this y (see video_engine.CAPTION_ZONE_BOTTOM) — the
# watermark lives in the gap between there and the bottom of the frame, so the two
# never fight for space.
SAFE_ZONE_TOP = 1800
FRAME_BOTTOM_MARGIN = 20  # never let the watermark's own edge touch the frame edge


def _render_watermark_image(font_path: str) -> Image.Image:
    """Render the watermark text onto a canvas sized exactly to its measured bounding
    box (plus padding) — sizing the canvas from the real glyph bounds, rather than a
    guessed width/height, is what guarantees no clipping regardless of font metrics."""
    font = ImageFont.truetype(font_path, WATERMARK_FONT_SIZE)
    probe = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    bbox = ImageDraw.Draw(probe).textbbox((0, 0), WATERMARK_TEXT, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    canvas = Image.new("RGBA", (text_w + WATERMARK_PADDING * 2, text_h + WATERMARK_PADDING * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text((WATERMARK_PADDING - bbox[0], WATERMARK_PADDING - bbox[1]), WATERMARK_TEXT, font=font, fill=WATERMARK_COLOR)
    return canvas


def _compute_position(wm_w: int, wm_h: int) -> tuple[int, int]:
    """Center the watermark horizontally, and vertically center it within the safe
    band below the caption zone — clamped so it can never cross the frame's bottom
    edge (which is what would produce a half-cut-off watermark)."""
    if wm_w > VIDEO_SIZE[0] or wm_h > (VIDEO_SIZE[1] - SAFE_ZONE_TOP):
        raise RuntimeError(
            f"Watermark ({wm_w}x{wm_h}) doesn't fit in the safe zone below captions "
            f"({VIDEO_SIZE[0]}x{VIDEO_SIZE[1] - SAFE_ZONE_TOP}) — shrink WATERMARK_FONT_SIZE."
        )

    x = round((VIDEO_SIZE[0] - wm_w) / 2)
    available = VIDEO_SIZE[1] - FRAME_BOTTOM_MARGIN - SAFE_ZONE_TOP
    y = round(SAFE_ZONE_TOP + max(0, (available - wm_h) / 2))
    y = min(y, VIDEO_SIZE[1] - FRAME_BOTTOM_MARGIN - wm_h)
    return x, y


def add_watermark(video_path: Path, font_path: str) -> None:
    """Overlay the EdAI watermark onto an already-rendered draft video, in place.

    Pure local video compositing on an existing file — no TTS/LLM calls, no cost.
    """
    watermark_img = _render_watermark_image(font_path)
    watermark_array = np.array(watermark_img)
    wm_w, wm_h = watermark_img.size
    x, y = _compute_position(wm_w, wm_h)

    clip = VideoFileClip(str(video_path))
    watermark_clip = ImageClip(watermark_array).with_position((x, y)).with_duration(clip.duration)
    result = CompositeVideoClip([clip, watermark_clip], size=VIDEO_SIZE).with_audio(clip.audio)

    tmp_path = video_path.with_name(video_path.stem + ".watermark_tmp" + video_path.suffix)
    # moviepy writes its own intermediate audio temp file into the current working
    # directory by default, regardless of the output path — pin it next to the real
    # output instead so a run never drops stray files at the repo root.
    result.write_videofile(
        str(tmp_path),
        fps=clip.fps,
        codec="libx264",
        audio_codec="aac",
        logger=None,
        temp_audiofile_path=str(video_path.parent),
    )

    result.close()
    clip.close()
    watermark_clip.close()

    tmp_path.replace(video_path)


@app.command()
def run(
    drafts_dir: Path = typer.Option(Path("data/drafts"), help="Directory of video_engine drafts"),
) -> None:
    """Stamp the EdAI watermark onto every draft.mp4 in drafts_dir, in place. Free — no API calls."""
    settings = get_settings()
    font = _resolve_font(settings)

    video_paths = sorted(drafts_dir.glob("*/draft.mp4"))
    if not video_paths:
        console.print(f"[yellow]No draft.mp4 files found under {drafts_dir}[/yellow]")
        return

    for path in video_paths:
        console.print(f"Watermarking {path}...")
        add_watermark(path, font)

    console.print(f"[bold green]Done.[/bold green] Watermarked {len(video_paths)} video(s).")


if __name__ == "__main__":
    app()
