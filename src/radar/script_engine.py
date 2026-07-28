from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import typer
from anthropic import Anthropic
from rich.console import Console
from rich.table import Table

from radar.config import Settings, get_settings
from radar.costs import record_anthropic_usage
from radar.models import EvidenceLedgerEntry, Problem, Script, ScriptSection

app = typer.Typer(add_completion=False)
console = Console()


class ScriptValidationError(Exception):
    """Raised when a generated script fails the evidence-ledger or pacing gate."""

# The EdAI formula — fixed beats, not left to the model to decide.
# Modeled on YC's Request for Startups structure: Problem -> Why it matters -> Why now -> invitation.
# "Why Now" gets the most time (20s) because live testing showed it's structurally the densest
# beat — it has to cover both "why this has been hard" and "what's changed" — and consistently
# overshot every other section's budget combined. The real product ceiling (<60s) is enforced
# separately by TOTAL_DURATION_CAP_SEC below, not by this per-section sum staying small.
SECTION_SPECS: list[tuple[str, int, int]] = [
    ("Hook", 0, 4),
    ("Why It Matters", 4, 14),
    ("Why Now", 14, 34),
    ("Opportunity", 34, 46),
    ("Teen Challenge", 46, 56),
]
SECTION_NAMES = [name for name, _, _ in SECTION_SPECS]

# Sections that must carry at least one cited claim — the strict evidence-ledger gate.
FACT_SECTIONS = {"Why It Matters", "Why Now", "Opportunity"}

WORDS_PER_SEC = 2.60  # measured ElevenLabs (Liam, eleven_multilingual_v2) pace, not just a guess
PACING_TOLERANCE = 1.15  # allowed slack over the ideal per-section word budget before hard-failing
TOTAL_DURATION_CAP_SEC = 58  # hard outer bound (2s under the 60s requirement), no tolerance padding


def _build_tool(num_citations: int) -> dict:
    citation_index_schema = {"type": "integer", "minimum": 0, "maximum": max(num_citations - 1, 0)}
    return {
        "name": "emit_script",
        "description": "Emit a 60-90 second vertical video script following the fixed 5-beat EdAI formula.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sections": {
                    "type": "array",
                    "minItems": 5,
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "enum": SECTION_NAMES},
                            "lines": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "citation_indices": {
                                            "type": "array",
                                            "description": "Indices into the provided citation list this line's claim is grounded in. Empty for non-factual lines (hooks, transitions, calls-to-action).",
                                            "items": citation_index_schema,
                                        },
                                    },
                                    "required": ["text", "citation_indices"],
                                },
                            },
                        },
                        "required": ["name", "lines"],
                    },
                }
            },
            "required": ["sections"],
        },
    }


def _section_word_target(duration_sec: int) -> int:
    return max(1, round(duration_sec * WORDS_PER_SEC))


def _system_prompt() -> str:
    section_budgets = "\n".join(
        f"  - {name}: aim for ~{_section_word_target(end - start)} words, NEVER exceed "
        f"{_section_word_budget(end - start)} words ({end - start}s)"
        for name, start, end in SECTION_SPECS
    )
    return f"""You are a scriptwriter for EdAI's World View program. You turn a researched, cited problem \
into a punchy, under-60-second vertical (9:16) video script for a teenage audience — modeled on the tone and \
structure of Y Combinator's Requests for Startups: confident, second person ("you"), concrete numbers instead \
of vague claims, no filler, no hedging.

Follow this fixed 5-beat structure exactly, one section per beat, in this order:
1. Hook (~0-4s) — a striking question or statement that grabs attention in the first second. No citation needed.
2. Why It Matters (~4-14s) — state the problem's real scale with a hard number, RFS-style ("Problem" + "Why it \
matters"). Grounded in real evidence.
3. Why Now (~14-34s) — this is the RFS "why now" beat: what's changed that makes this solvable today (new tech, \
a shift, an opening). One short clause acknowledging the barrier is enough — most of this section should be the \
pivot to what's new, not a recap of why it's been hard. Grounded in real evidence.
4. Opportunity (~34-46s) — what a solution could actually look like. At least one line here should be grounded \
in real evidence (e.g. an existing technology or trend the citations mention) — but lines that are your own \
hypothetical sketch of a solution ("imagine a tool that...") are not facts and should NOT carry a citation just \
to satisfy the rule below; leave those empty rather than misattaching a source to an idea it doesn't verify.
5. Teen Challenge (~46-56s) — a warm, direct, YC-style invitation: not "go look this up," but something closer \
to "if this is the kind of problem you want to spend real time on, this is where you start." No citation needed.

Hard rules:
- You will be given a numbered list of citations (quote + url) already verified by the research team. \
These are the ONLY sources you may cite. Reference them by index only.
- Every line that states a fact, statistic, or claim MUST include at least one citation_indices entry. \
Never invent a citation index that wasn't provided, and never state a specific fact with an empty \
citation_indices list.
- Lines that are purely rhetorical, transitional, or a call-to-action (typically in Hook and Teen Challenge) \
should have an empty citation_indices list — don't force a citation onto them.
- Only attach a citation_indices entry to a line if that specific citation's quote actually verifies THAT \
line's claim. Don't reuse one citation to prop up several different, unrelated claims just because a section \
needs "at least one" cited line — a section satisfies this with a single line that's genuinely, tightly \
grounded; the rest of that section's lines can be uncited if they're inference, framing, or a hypothetical \
solution rather than a sourced fact. A citation used honestly on one line beats the same citation stretched \
across five.
- HARD LIMIT: each section's total spoken word count (summed across its lines) must not exceed its "NEVER \
exceed" ceiling below. Writers reliably run a bit longer than they estimate — so treat the "aim for" number \
as your actual target, not the ceiling. If you're tempted to hit the ceiling exactly, cut a word instead. \
This is measured against real narration speed (~{WORDS_PER_SEC} words/sec) and enforced by code \
after you respond — going over means the whole script gets rejected, not just marked down.
{section_budgets}
- ALSO HARD LIMIT: the whole script (all sections combined) must not exceed {_total_word_cap()} words total \
— even if every individual section is within its own ceiling above, the script is rejected if the sum is over \
this. Treat the per-section ceilings as the max for that section alone, not as a budget you're entitled to \
spend in full on every section.
- Write for a teenage audience: clear, direct, no jargon. Short sentences. Every line should sound like it \
could be spoken out loud in one breath.
"""


def _load_problem(path: Path, index: int | None, title: str | None) -> Problem:
    raw_items = json.loads(path.read_text())
    problems = [Problem(**item) for item in raw_items]

    if title:
        matches = [p for p in problems if title.lower() in p.title.lower()]
        if not matches:
            raise typer.BadParameter(f"No problem title matching {title!r} found in {path}")
        return matches[0]

    if index is not None:
        return problems[index]

    return max(problems, key=lambda p: p.total_score)


def _parse_sections_input(sections_input: object) -> dict[str, list]:
    """Turn the model's raw `sections` tool-input into {section_name: lines}, recovering from
    the double-encoded-JSON-string failure mode instead of discarding an otherwise-good payload.
    """
    if isinstance(sections_input, str):
        try:
            parsed = json.loads(sections_input)
        except json.JSONDecodeError as e:
            raise ScriptValidationError(f"Model returned an unparsable script payload: {e}") from e
        sections_input = parsed["sections"] if isinstance(parsed, dict) and "sections" in parsed else parsed

    try:
        return {s["name"]: s["lines"] for s in sections_input}
    except (TypeError, KeyError) as e:
        raise ScriptValidationError(f"Model returned a malformed script payload: {e}") from e


def _generate_sections(problem: Problem, settings: Settings) -> list[ScriptSection]:
    client = Anthropic(api_key=settings.anthropic_api_key)
    citations_block = "\n".join(f"[{i}] \"{c.quote}\" — {c.url}" for i, c in enumerate(problem.citations))

    user_message = (
        f"Problem: {problem.title}\n"
        f"Domain: {problem.domain}\n"
        f"Summary: {problem.summary}\n\n"
        f"Available citations (reference ONLY by index):\n{citations_block}"
    )

    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=_system_prompt(),
        tools=[_build_tool(len(problem.citations))],
        tool_choice={"type": "tool", "name": "emit_script"},
        messages=[{"role": "user", "content": user_message}],
    )

    record_anthropic_usage(
        "script_generate", settings.anthropic_model, message.usage.input_tokens, message.usage.output_tokens
    )

    tool_use = next(block for block in message.content if block.type == "tool_use")
    raw_sections = _parse_sections_input(tool_use.input["sections"])

    missing = set(SECTION_NAMES) - set(raw_sections)
    if missing:
        raise ScriptValidationError(f"Model omitted required section(s): {sorted(missing)}")

    sections = []
    for name, start_sec, end_sec in SECTION_SPECS:
        for line in raw_sections[name]:
            for idx in line.get("citation_indices", []):
                if not (0 <= idx < len(problem.citations)):
                    raise ScriptValidationError(
                        f"Section {name!r} references citation index {idx}, "
                        f"but only {len(problem.citations)} citations were provided."
                    )
        sections.append(
            ScriptSection(name=name, start_sec=start_sec, end_sec=end_sec, lines=raw_sections[name])
        )

    return sections


def _build_evidence_ledger(problem: Problem, sections: list[ScriptSection]) -> list[EvidenceLedgerEntry]:
    used_in: dict[int, list[str]] = {}
    for section in sections:
        for line in section.lines:
            for idx in line.citation_indices:
                used_in.setdefault(idx, []).append(f"{section.name}: {line.text}")

    return [
        EvidenceLedgerEntry(citation=problem.citations[idx], used_in=usages)
        for idx, usages in sorted(used_in.items())
    ]


def _validate_evidence_ledger(sections: list[ScriptSection]) -> None:
    uncited_fact_sections = []
    for section in sections:
        if section.name not in FACT_SECTIONS:
            continue
        has_citation = any(line.citation_indices for line in section.lines)
        if not has_citation:
            uncited_fact_sections.append(section.name)

    if uncited_fact_sections:
        raise ScriptValidationError(
            "Evidence ledger validation failed. "
            f"These sections make claims with zero linked citations: {', '.join(uncited_fact_sections)}"
        )


def _section_word_budget(duration_sec: int) -> int:
    return max(1, round(duration_sec * WORDS_PER_SEC * PACING_TOLERANCE))


def _total_word_cap() -> int:
    # The real product requirement — no per-section tolerance padding here, since this is
    # the outer bound that actually determines rendered duration regardless of how words
    # are distributed across sections.
    return round(TOTAL_DURATION_CAP_SEC * WORDS_PER_SEC)


def _validate_pacing(sections: list[ScriptSection]) -> None:
    overruns = []
    for section in sections:
        duration = section.end_sec - section.start_sec
        budget = _section_word_budget(duration)
        word_count = sum(len(line.text.split()) for line in section.lines)
        if word_count > budget:
            overruns.append(f"{section.name}: {word_count} words (budget ~{budget} for {duration}s)")

    if overruns:
        raise ScriptValidationError(
            "Pacing validation failed. "
            f"These sections have too many words for their allotted time at ~{WORDS_PER_SEC} words/sec:\n  "
            + "\n  ".join(overruns)
        )

    total_words = sum(len(line.text.split()) for section in sections for line in section.lines)
    total_cap = _total_word_cap()
    if total_words > total_cap:
        raise ScriptValidationError(
            f"Pacing validation failed. Total script is {total_words} words — over the "
            f"{TOTAL_DURATION_CAP_SEC}s hard cap (~{total_cap} words at ~{WORDS_PER_SEC} words/sec), "
            "even though individual sections were within their own budgets."
        )


def generate_script(problem: Problem, settings: Settings, source_path: str) -> Script:
    """Generate and validate a script for a problem. Raises ScriptValidationError on gate failure."""
    sections = _generate_sections(problem, settings)
    _validate_evidence_ledger(sections)
    _validate_pacing(sections)

    return Script(
        problem_title=problem.title,
        domain=problem.domain,
        source_path=source_path,
        sections=sections,
        evidence_ledger=_build_evidence_ledger(problem, sections),
    )


def _save(script: Script, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = script.problem_title.lower().replace(" ", "_").replace("/", "_")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"{slug}_{timestamp}.json"
    path.write_text(json.dumps(script.model_dump(), indent=2))
    return path


def _render_script(script: Script) -> None:
    for section in script.sections:
        console.print(
            f"\n[bold underline]{section.name}[/bold underline] "
            f"({section.start_sec}-{section.end_sec}s)"
        )
        for line in section.lines:
            marker = "".join(f"[{i}]" for i in line.citation_indices)
            console.print(f"  {line.text} {marker}".rstrip())

    table = Table(title="\nEvidence Ledger", show_lines=True)
    table.add_column("Citation", style="bold")
    table.add_column("Used In")

    for i, entry in enumerate(script.evidence_ledger):
        quote = entry.citation.quote
        used_in = "\n".join(entry.used_in)
        table.add_row(f'[{i}] "{quote}"\n{entry.citation.url}', used_in)

    console.print(table)


@app.command()
def run(
    input_path: Path = typer.Argument(..., help="Path to a research_engine output JSON in data/raw/"),
    index: int | None = typer.Option(None, help="Select the problem by index (0-based) in the file"),
    title: str | None = typer.Option(None, help="Select the problem by a title substring match"),
    output_dir: Path = typer.Option(Path("data/scripts"), help="Where to save the script JSON"),
) -> None:
    """Generate a 60-90s EdAI-formula script for a selected problem, with a strict evidence ledger."""
    settings = get_settings()

    problem = _load_problem(input_path, index, title)
    console.print(f"[bold]Selected problem:[/bold] {problem.title} (total score {problem.total_score})")

    console.print("[bold]Generating script...[/bold]")
    try:
        script = generate_script(problem, settings, str(input_path))
    except ScriptValidationError as e:
        console.print(f"[bold red]{e}[/bold red]")
        raise typer.Exit(1) from e

    path = _save(script, output_dir)
    console.print(f"[bold]Saved:[/bold] {path}")

    _render_script(script)


if __name__ == "__main__":
    app()
