from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import typer
from anthropic import Anthropic
from requests.exceptions import RequestException
from rich.console import Console
from rich.table import Table
from tavily import TavilyClient

from radar.config import Settings, get_settings
from radar.costs import record_anthropic_usage
from radar.models import Problem

app = typer.Typer(add_completion=False)
console = Console()


class Domain(str, Enum):
    """The 10 focus domains of the EdAI World View program."""

    AEROSPACE = "Aerospace and Space Systems"
    DEFENSE = "Defense"
    AGRICULTURE = "Agriculture"
    ENERGY = "Energy"
    PUBLIC_SAFETY = "Public Safety and Emergency Response"
    SUPPLY_CHAIN = "Supply Chain and Critical Logistics"
    INDUSTRIALS = "Industrials, Manufacturing and Small and Medium-Sized Enterprises"
    EDUCATION = "Education"
    HEALTHCARE = "Healthcare"
    HOUSING = "Housing"


class NoResultsError(Exception):
    """Raised when a domain search returns no usable sources."""


QUERY_TEMPLATES = [
    "biggest unsolved problems in {domain}",
    "{domain} industry challenges and risks",
    "{domain} failures statistics 2024 2025",
    "urgent problems facing {domain}",
]

EXTRACT_TOOL = {
    "name": "emit_problem_candidates",
    "description": "Emit exactly 5 real-world problem candidates for the given domain, each grounded in the provided sources.",
    "input_schema": {
        "type": "object",
        "properties": {
            "problems": {
                "type": "array",
                "minItems": 5,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "summary": {
                            "type": "string",
                            "description": "2-3 sentence description of the problem",
                        },
                        "citations": {
                            "type": "array",
                            "minItems": 2,
                            "description": (
                                "At least 2 citations from genuinely different source URLs, each backing a "
                                "different facet of the problem — not the same URL twice, and not two quotes "
                                "restating the same fact."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "url": {"type": "string"},
                                    "quote": {
                                        "type": "string",
                                        "description": "Short, near-verbatim excerpt copied from the source content provided — never fabricated.",
                                    },
                                    "source_title": {"type": "string"},
                                },
                                "required": ["url", "quote"],
                            },
                        },
                        "scores": {
                            "type": "object",
                            "properties": {
                                "consequence": {"type": "integer", "minimum": 1, "maximum": 10},
                                "urgency": {"type": "integer", "minimum": 1, "maximum": 10},
                                "neglect": {"type": "integer", "minimum": 1, "maximum": 10},
                                "teen_accessibility": {"type": "integer", "minimum": 1, "maximum": 10},
                            },
                            "required": ["consequence", "urgency", "neglect", "teen_accessibility"],
                        },
                        "score_rationale": {"type": "string"},
                    },
                    "required": ["title", "summary", "citations", "scores"],
                },
            }
        },
        "required": ["problems"],
    },
}

SYSTEM_PROMPT = """You are a research analyst sourcing real-world problems for an education/content team.

You will be given raw search snippets (title, url, content, and sometimes a publish date) about a domain. \
From these snippets ONLY, extract exactly 5 distinct, real-world problem candidates.

Hard rules:
- Every citation's `quote` must be a short, near-verbatim excerpt copied from the provided source content. \
Never invent a statistic, quote, or claim that isn't traceable to the given material.
- Every citation's `url` must be copied exactly from the provided sources — never invent or guess a URL.
- Each problem needs AT LEAST 2 citations from genuinely DIFFERENT source URLs, each supporting a DIFFERENT \
facet of the problem (e.g. one on scale/impact, another on cause or a specific data point) — not two quotes \
saying the same thing, and not the same fact split across two citation entries. This gives the downstream \
scriptwriter enough distinct evidence to avoid stretching one quote to justify several unrelated claims.
- If the provided sources don't support 5 distinct problems each with 2+ genuinely different citations, pick \
the most defensible ones you can and keep citations honest rather than padding with unsupported claims.

Score each problem 1-10 on four axes:
- Consequence: how severe/large-scale the impact is if the problem stays unsolved.
- Urgency: how time-sensitive the problem is right now.
- Neglect: how underexplored or underfunded the problem is relative to its importance.
- Teen Accessibility: how understandable and engaging this problem is for a teenage audience.
"""


TAVILY_MAX_RETRIES = 3
TAVILY_RETRY_BASE_DELAY = 2.0  # seconds, doubles each retry


def _search_with_retry(client: TavilyClient, query: str) -> dict:
    """Tavily's advanced search occasionally times out under load; retry with backoff
    rather than letting a single transient network error crash the whole domain run."""
    for attempt in range(TAVILY_MAX_RETRIES):
        try:
            return client.search(query=query, search_depth="advanced", max_results=5)
        except (TimeoutError, RequestException):
            if attempt == TAVILY_MAX_RETRIES - 1:
                raise
            time.sleep(TAVILY_RETRY_BASE_DELAY * (2**attempt))
    raise AssertionError("unreachable")  # loop always returns or raises


def _search_domain(domain: str, settings: Settings) -> list[dict]:
    client = TavilyClient(api_key=settings.tavily_api_key)
    seen_urls: set[str] = set()
    results: list[dict] = []

    for template in QUERY_TEMPLATES:
        query = template.format(domain=domain)
        response = _search_with_retry(client, query)
        for item in response.get("results", []):
            url = item.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            results.append(item)

    return results


def _build_source_block(results: list[dict]) -> str:
    lines = []
    for i, item in enumerate(results, start=1):
        title = item.get("title", "")
        url = item.get("url", "")
        content = (item.get("content") or "")[:800]
        published = item.get("published_date", "")
        lines.append(f"[{i}] {title}\nURL: {url}\nPublished: {published}\nContent: {content}\n")
    return "\n".join(lines)


def _extract_and_score(domain: str, results: list[dict], settings: Settings) -> list[Problem]:
    client = Anthropic(api_key=settings.anthropic_api_key)
    source_block = _build_source_block(results)

    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "emit_problem_candidates"},
        messages=[
            {
                "role": "user",
                "content": f"Domain: {domain}\n\nSources:\n\n{source_block}",
            }
        ],
    )

    record_anthropic_usage(
        "research_extract", settings.anthropic_model, message.usage.input_tokens, message.usage.output_tokens
    )

    tool_use = next(block for block in message.content if block.type == "tool_use")
    raw_problems = tool_use.input["problems"]

    return [Problem(domain=domain, **raw) for raw in raw_problems]


def _has_distinct_citation_sources(problem: Problem) -> bool:
    urls = {c.url for c in problem.citations}
    return len(urls) >= 2


def _enforce_citation_diversity(problems: list[Problem]) -> list[Problem]:
    """Drop problems whose citations don't actually come from 2+ distinct URLs.

    The prompt asks the model for this, but models don't reliably comply — this is
    the code-level backstop so a single-source (or duplicate-source) problem never
    reaches script_engine, where it would pressure the model into stretching one
    citation across unrelated claims.
    """
    kept = []
    for p in problems:
        if _has_distinct_citation_sources(p):
            kept.append(p)
        else:
            urls = [c.url for c in p.citations]
            console.print(
                f"[yellow]Dropping '{p.title}' — citations don't come from 2+ distinct "
                f"sources ({urls}).[/yellow]"
            )
    return kept


def _save(domain: str, problems: list[Problem], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = domain.lower().replace(" ", "_").replace("/", "_")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"{slug}_{timestamp}.json"
    path.write_text(json.dumps([p.model_dump() for p in problems], indent=2))
    return path


def _render_table(problems: list[Problem]) -> None:
    table = Table(title="Problem Candidates", show_lines=True)
    table.add_column("Title", style="bold")
    table.add_column("Consq", justify="right")
    table.add_column("Urg", justify="right")
    table.add_column("Negl", justify="right")
    table.add_column("Teen", justify="right")
    table.add_column("Total", justify="right", style="bold green")
    table.add_column("Sources", justify="right")

    for p in sorted(problems, key=lambda p: p.total_score, reverse=True):
        s = p.scores
        table.add_row(
            p.title,
            str(s.consequence),
            str(s.urgency),
            str(s.neglect),
            str(s.teen_accessibility),
            str(p.total_score),
            str(len(p.citations)),
        )

    console.print(table)


def research_domain(domain: str, settings: Settings) -> list[Problem]:
    """Search + score a domain's problem candidates. Raises NoResultsError if search finds nothing."""
    console.print(f"[bold]Searching sources for:[/bold] {domain}")
    results = _search_domain(domain, settings)
    if not results:
        raise NoResultsError(f"No search results found for {domain!r} — try a different domain phrasing.")
    console.print(f"Found {len(results)} unique sources.")

    console.print("[bold]Extracting and scoring problem candidates...[/bold]")
    problems = _extract_and_score(domain, results, settings)

    problems = _enforce_citation_diversity(problems)
    if not problems:
        raise NoResultsError(
            f"All problem candidates for {domain!r} failed the 2-distinct-source citation "
            "check — try re-running (sources vary per search) or a different domain phrasing."
        )

    return problems


@app.command()
def run(
    domain: Domain = typer.Argument(..., help="One of the 10 EdAI World View program domains"),
    output_dir: Path = typer.Option(Path("data/raw"), help="Where to save the JSON output"),
) -> None:
    """Research a domain and output 5 scored, cited problem candidates."""
    settings = get_settings()
    domain = domain.value

    try:
        problems = research_domain(domain, settings)
    except NoResultsError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    path = _save(domain, problems, output_dir)
    console.print(f"[bold]Saved:[/bold] {path}")

    _render_table(problems)

    for p in sorted(problems, key=lambda p: p.total_score, reverse=True):
        console.print(f"\n[bold underline]{p.title}[/bold underline]  (total {p.total_score})")
        console.print(p.summary)
        for c in p.citations:
            console.print(f'  - "{c.quote}" — {c.url}')


if __name__ == "__main__":
    app()
