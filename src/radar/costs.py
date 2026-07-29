from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False)
console = Console()

# Pricing as of 2026-07 (Anthropic first-party API rates; ElevenLabs is a blended
# estimate — actual cost depends on your plan tier). Verify before relying on this
# for real budgeting; these numbers will drift as pricing changes.
ANTHROPIC_PRICE_PER_MTOK = {
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
}
ELEVENLABS_PRICE_PER_1K_CHARS = 0.18

DEFAULT_LOG_PATH = Path("data/usage.jsonl")


def _append(log_path: Path, fields: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **fields}
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def record_anthropic_usage(
    operation: str, model: str, input_tokens: int, output_tokens: int, log_path: Path = DEFAULT_LOG_PATH
) -> float:
    prices = ANTHROPIC_PRICE_PER_MTOK.get(model, ANTHROPIC_PRICE_PER_MTOK["claude-sonnet-5"])
    cost = (input_tokens / 1_000_000) * prices["input"] + (output_tokens / 1_000_000) * prices["output"]
    _append(
        log_path,
        {
            "provider": "anthropic",
            "operation": operation,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost, 6),
        },
    )
    return cost


def record_tts_usage(operation: str, characters: int, log_path: Path = DEFAULT_LOG_PATH) -> float:
    cost = (characters / 1000) * ELEVENLABS_PRICE_PER_1K_CHARS
    _append(
        log_path,
        {"provider": "tts", "operation": operation, "characters": characters, "cost_usd": round(cost, 6)},
    )
    return cost


# Rough per-video estimate for the pre-flight confirmation in `radar batch`, based on
# typical observed usage in this pipeline (script generation often needs 2-3 attempts
# to pass the pacing/evidence-ledger gates).
_EST_SCRIPT_INPUT_TOK = 1800
_EST_SCRIPT_OUTPUT_TOK = 900
_EST_SCRIPT_ATTEMPTS = 2
_EST_ICON_QUERY_INPUT_TOK = 500
_EST_ICON_QUERY_OUTPUT_TOK = 300
_EST_RESEARCH_INPUT_TOK = 3000
_EST_RESEARCH_OUTPUT_TOK = 1500

# Observed average narration length across many generated scripts (range was
# roughly 620-940 chars/video) — used to estimate ElevenLabs TTS cost per video.
_EST_CHARS_PER_VIDEO = 820


def estimate_batch_cost(count: int, model: str = "claude-sonnet-5") -> dict:
    prices = ANTHROPIC_PRICE_PER_MTOK.get(model, ANTHROPIC_PRICE_PER_MTOK["claude-sonnet-5"])

    def anthropic_cost(input_tok: float, output_tok: float) -> float:
        return (input_tok / 1_000_000) * prices["input"] + (output_tok / 1_000_000) * prices["output"]

    research = anthropic_cost(_EST_RESEARCH_INPUT_TOK, _EST_RESEARCH_OUTPUT_TOK)
    per_video_script = anthropic_cost(
        _EST_SCRIPT_INPUT_TOK * _EST_SCRIPT_ATTEMPTS, _EST_SCRIPT_OUTPUT_TOK * _EST_SCRIPT_ATTEMPTS
    )
    per_video_icons = anthropic_cost(_EST_ICON_QUERY_INPUT_TOK, _EST_ICON_QUERY_OUTPUT_TOK)

    anthropic_total = research + count * (per_video_script + per_video_icons)
    elevenlabs_total = count * (_EST_CHARS_PER_VIDEO / 1000) * ELEVENLABS_PRICE_PER_1K_CHARS

    return {
        "count": count,
        "anthropic_usd": round(anthropic_total, 3),
        "elevenlabs_usd": round(elevenlabs_total, 3),
        "total_usd": round(anthropic_total + elevenlabs_total, 3),
    }


@app.command()
def show(log_path: Path = typer.Option(DEFAULT_LOG_PATH, help="Path to the usage log")) -> None:
    """Show cumulative API spend recorded so far."""
    if not log_path.exists():
        console.print("No usage recorded yet.")
        return

    entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]

    by_provider: dict[str, float] = {}
    for e in entries:
        by_provider[e["provider"]] = by_provider.get(e["provider"], 0.0) + e.get("cost_usd", 0.0)

    table = Table(title="Cumulative API Spend")
    table.add_column("Provider", style="bold")
    table.add_column("Calls", justify="right")
    table.add_column("Cost (USD)", justify="right")

    for provider, cost in by_provider.items():
        count = sum(1 for e in entries if e["provider"] == provider)
        table.add_row(provider, str(count), f"${cost:.4f}")

    total = sum(by_provider.values())
    table.add_row("[bold]Total[/bold]", str(len(entries)), f"[bold]${total:.4f}[/bold]")

    console.print(table)


if __name__ == "__main__":
    app()
