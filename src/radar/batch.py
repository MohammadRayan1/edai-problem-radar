from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from radar.config import get_settings
from radar.costs import estimate_batch_cost
from radar.research_engine import Domain, NoResultsError, research_domain
from radar.research_engine import _save as save_research
from radar.script_engine import ScriptValidationError, generate_script
from radar.script_engine import _save as save_script
from radar.video_engine import generate_video

app = typer.Typer(add_completion=False)
console = Console()

MAX_SCRIPT_ATTEMPTS = 3


@app.command()
def run(
    domain: Domain = typer.Argument(..., help="One of the 10 EdAI World View program domains"),
    count: int = typer.Option(5, min=1, max=5, help="How many of the domain's problems to turn into videos"),
    raw_dir: Path = typer.Option(Path("data/raw"), help="Where to save the research JSON"),
    scripts_dir: Path = typer.Option(Path("data/scripts"), help="Where to save script JSONs"),
    drafts_dir: Path = typer.Option(Path("data/drafts"), help="Where to save draft videos"),
    voice: str | None = typer.Option(None, help="ElevenLabs voice_id to use (overrides config default)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the cost-estimate confirmation prompt"),
) -> None:
    """Research a domain once, then generate a script + draft video for its top N problems."""
    settings = get_settings()
    domain_name = domain.value

    estimate = estimate_batch_cost(count, settings.anthropic_model)
    console.print(
        f"[bold]Estimated cost for {count} video(s):[/bold] ~${estimate['total_usd']:.2f} "
        f"(${estimate['anthropic_usd']:.2f} Anthropic + ${estimate['elevenlabs_usd']:.2f} ElevenLabs — "
        "rough estimate, actual cost is logged as it runs; see `radar usage show`)"
    )
    if not yes and not typer.confirm("Proceed?", default=True):
        raise typer.Exit(0)

    try:
        problems = research_domain(domain_name, settings)
    except NoResultsError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    raw_path = save_research(domain_name, problems, raw_dir)
    console.print(f"[bold]Saved research:[/bold] {raw_path}\n")

    selected = sorted(problems, key=lambda p: p.total_score, reverse=True)[:count]

    results = []
    for i, problem in enumerate(selected, start=1):
        console.print(f"[bold cyan]--- [{i}/{len(selected)}] {problem.title} ---[/bold cyan]")

        script = None
        last_error: Exception | None = None
        for attempt in range(1, MAX_SCRIPT_ATTEMPTS + 1):
            try:
                script = generate_script(problem, settings, str(raw_path))
                break
            except ScriptValidationError as e:
                last_error = e
                console.print(f"[yellow]Script attempt {attempt}/{MAX_SCRIPT_ATTEMPTS} failed: {e}[/yellow]")

        if script is None:
            console.print(f"[bold red]Skipping {problem.title!r} — script never passed validation.[/bold red]\n")
            results.append({"problem": problem.title, "status": "skipped", "detail": str(last_error)})
            continue

        script_path = save_script(script, scripts_dir)
        console.print(f"Saved script: {script_path}")

        try:
            video_path = generate_video(script, script_path, settings, drafts_dir, voice)
        except Exception as e:  # noqa: BLE001 - one bad video shouldn't kill the batch
            console.print(f"[bold red]Video generation failed for {problem.title!r}: {e}[/bold red]\n")
            results.append({"problem": problem.title, "status": "video_failed", "detail": str(e)})
            continue

        results.append({"problem": problem.title, "status": "done", "detail": str(video_path)})
        console.print()

    table = Table(title="Batch Summary", show_lines=True)
    table.add_column("Problem", style="bold")
    table.add_column("Status")
    table.add_column("Detail")

    style = {"done": "bold green", "skipped": "yellow", "video_failed": "bold red"}
    for r in results:
        table.add_row(r["problem"], f"[{style[r['status']]}]{r['status']}[/{style[r['status']]}]", r["detail"])

    console.print(table)
    console.print("\nRun `radar review list` to review the generated drafts.")


if __name__ == "__main__":
    app()
