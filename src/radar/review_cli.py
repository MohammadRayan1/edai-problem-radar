from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlmodel import Session, select

from radar.storage import ReviewRecord, get_engine, now_iso

app = typer.Typer(add_completion=False)
console = Console()

STATUS_STYLE = {
    "pending": "yellow",
    "approved": "bold green",
    "rejected": "bold red",
    "changes_requested": "cyan",
}


def _sync_drafts(session: Session, drafts_dir: Path) -> None:
    if not drafts_dir.exists():
        return

    for draft_dir in sorted(drafts_dir.iterdir()):
        meta_path = draft_dir / "meta.json"
        if not meta_path.exists():
            continue

        existing = session.exec(
            select(ReviewRecord).where(ReviewRecord.draft_dir == str(draft_dir))
        ).first()
        if existing:
            continue

        meta = json.loads(meta_path.read_text())
        session.add(
            ReviewRecord(
                draft_dir=str(draft_dir),
                problem_title=meta["problem_title"],
                domain=meta["domain"],
                video_path=meta["video_path"],
                meta_path=str(meta_path),
                script_path=meta["script_path"],
                total_duration_seconds=meta["total_duration_seconds"],
            )
        )

    session.commit()


def _get_record(session: Session, item_id: int) -> ReviewRecord:
    record = session.get(ReviewRecord, item_id)
    if record is None:
        console.print(f"[bold red]No draft with id {item_id}.[/bold red] Run `list` to see available drafts.")
        raise typer.Exit(1)
    return record


# A citation reused across many lines is often a sign it's being stretched to justify claims
# it doesn't actually verify (common in "Opportunity" lines describing a hypothetical solution).
# This is a heuristic flag for a human reviewer, not a hard gate — some legitimately broad
# citations really do support several nearby claims.
CITATION_STRETCH_THRESHOLD = 3


def _evidence_warnings(script_data: dict) -> list[str]:
    warnings = []
    for i, entry in enumerate(script_data.get("evidence_ledger", [])):
        used_in = entry.get("used_in", [])
        if len(used_in) > CITATION_STRETCH_THRESHOLD:
            warnings.append(f"Citation [{i}] is used to back {len(used_in)} different lines — check each is actually verified by it, not just loosely inspired by it.")
    return warnings


@app.command("list")
def list_drafts(
    drafts_dir: Path = typer.Option(Path("data/drafts"), help="Directory of video_engine drafts"),
    db_path: Path = typer.Option(Path("data/radar.db"), help="Path to the review database"),
) -> None:
    """Scan for new drafts and show the approval queue."""
    engine = get_engine(db_path)
    with Session(engine) as session:
        _sync_drafts(session, drafts_dir)
        records = session.exec(select(ReviewRecord).order_by(ReviewRecord.id)).all()

    if not records:
        console.print("No drafts found yet. Run video_engine to produce one.")
        return

    table = Table(title="EdAI Problem Radar — Review Queue", show_lines=True)
    table.add_column("ID", justify="right")
    table.add_column("Title", style="bold")
    table.add_column("Domain")
    table.add_column("Duration")
    table.add_column("Status")
    table.add_column("Evidence")
    table.add_column("Video")

    for r in records:
        style = STATUS_STYLE.get(r.status, "white")
        evidence_flag = ""
        script_path = Path(r.script_path)
        if script_path.exists():
            script_data = json.loads(script_path.read_text())
            if _evidence_warnings(script_data):
                evidence_flag = "[bold yellow]⚠ stretched[/bold yellow]"
        table.add_row(
            str(r.id),
            r.problem_title,
            r.domain,
            f"{r.total_duration_seconds:.1f}s",
            f"[{style}]{r.status}[/{style}]",
            evidence_flag,
            r.video_path,
        )

    console.print(table)
    console.print(
        "\nUse `show <id>` for the full script + evidence ledger, "
        "then `approve <id>` / `reject <id> --note ...` / `request-changes <id> --note ...`."
    )


@app.command()
def show(
    item_id: int = typer.Argument(..., help="Draft ID from `list`"),
    db_path: Path = typer.Option(Path("data/radar.db")),
) -> None:
    """Show the full script, timing, and evidence ledger for a draft."""
    engine = get_engine(db_path)
    with Session(engine) as session:
        record = _get_record(session, item_id)

    console.print(
        Panel(
            f"[bold]{record.problem_title}[/bold]\n"
            f"Domain: {record.domain}\n"
            f"Duration: {record.total_duration_seconds:.1f}s\n"
            f"Video: {record.video_path}\n"
            f"Status: {record.status}"
        )
    )

    script_data = json.loads(Path(record.script_path).read_text())
    for section in script_data["sections"]:
        console.print(
            f"\n[bold underline]{section['name']}[/bold underline] "
            f"({section['start_sec']}-{section['end_sec']}s)"
        )
        for line in section["lines"]:
            marker = "".join(f"[{i}]" for i in line["citation_indices"])
            console.print(f"  {line['text']} {marker}".rstrip())

    ledger_table = Table(title="\nEvidence Ledger")
    ledger_table.add_column("Citation", style="bold")
    ledger_table.add_column("Used In")
    stretched = {
        i for i, entry in enumerate(script_data["evidence_ledger"])
        if len(entry["used_in"]) > CITATION_STRETCH_THRESHOLD
    }
    for i, entry in enumerate(script_data["evidence_ledger"]):
        quote = entry["citation"]["quote"]
        url = entry["citation"]["url"]
        used_in = "\n".join(entry["used_in"])
        label = f'[{i}] "{quote}"\n{url}'
        if i in stretched:
            label = f"[bold yellow]⚠ {label}[/bold yellow]"
            used_in = f"[bold yellow]{used_in}[/bold yellow]"
        ledger_table.add_row(label, used_in)
    console.print(ledger_table)

    warnings = _evidence_warnings(script_data)
    if warnings:
        console.print(
            Panel(
                "\n".join(f"⚠ {w}" for w in warnings),
                title="[bold yellow]Citation Stretch Warning[/bold yellow]",
                border_style="yellow",
            )
        )

    if record.notes:
        console.print(f"\n[bold]Notes:[/bold] {record.notes}")


def _decide(item_id: int, status: str, note: str | None, db_path: Path) -> None:
    engine = get_engine(db_path)
    with Session(engine) as session:
        record = _get_record(session, item_id)
        record.status = status
        record.notes = note
        record.decided_at = now_iso()
        session.add(record)
        session.commit()
        session.refresh(record)

    style = STATUS_STYLE.get(status, "white")
    console.print(f"[{style}]{record.problem_title} -> {status}[/{style}]")


@app.command()
def approve(
    item_id: int = typer.Argument(..., help="Draft ID from `list`"),
    db_path: Path = typer.Option(Path("data/radar.db")),
) -> None:
    """Approve a draft for downstream publishing."""
    _decide(item_id, "approved", None, db_path)


@app.command()
def reject(
    item_id: int = typer.Argument(..., help="Draft ID from `list`"),
    note: str = typer.Option(..., "--note", "-n", help="Why this draft is being rejected"),
    db_path: Path = typer.Option(Path("data/radar.db")),
) -> None:
    """Reject a draft."""
    _decide(item_id, "rejected", note, db_path)


@app.command("request-changes")
def request_changes(
    item_id: int = typer.Argument(..., help="Draft ID from `list`"),
    note: str = typer.Option(..., "--note", "-n", help="What should change before re-review"),
    db_path: Path = typer.Option(Path("data/radar.db")),
) -> None:
    """Send a draft back with notes for the next script_engine regeneration."""
    _decide(item_id, "changes_requested", note, db_path)


if __name__ == "__main__":
    app()
