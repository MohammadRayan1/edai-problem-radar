from __future__ import annotations

import typer

from radar import batch, costs, research_engine, review_cli, review_web, script_engine, video_engine, watermark

app = typer.Typer(add_completion=False, help="EdAI Problem Radar — research, script, video, review pipeline")

app.command("research", help="Find cited, scored problem candidates for a domain")(research_engine.run)
app.command("script", help="Generate a 60-90s EdAI-formula script from a researched problem")(script_engine.run)
app.command("video", help="Assemble a vertical draft video from a script")(video_engine.run)
app.command("batch", help="Research a domain and generate scripts + videos for its top N problems in one go")(
    batch.run
)
app.add_typer(review_cli.app, name="review", help="Review and approve/reject/request-changes on draft videos")
app.add_typer(costs.app, name="usage", help="Show cumulative API spend")
app.command(
    "review-web", help="Launch the browser-based review page (local-only, password-gated)"
)(review_web.serve)
app.command(
    "watermark", help="Stamp the EdAI watermark onto existing draft videos in place (free, no API calls)"
)(watermark.run)


if __name__ == "__main__":
    app()
