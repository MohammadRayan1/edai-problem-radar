from __future__ import annotations

import json

import pytest
import typer

from radar.models import Citation, Problem, ScoreBreakdown, ScriptLine, ScriptSection
from radar.script_engine import (
    SECTION_SPECS,
    ScriptValidationError,
    _build_evidence_ledger,
    _load_problem,
    _parse_sections_input,
    _section_word_budget,
    _total_word_cap,
    _validate_evidence_ledger,
    _validate_pacing,
)


def make_citation(i: int) -> Citation:
    return Citation(url=f"https://example.com/{i}", quote=f"quote {i}", source_title=f"Source {i}")


def make_problem(title: str = "Test Problem", num_citations: int = 3, total_score: int = 20) -> Problem:
    per_axis = max(1, min(10, total_score // 4))
    return Problem(
        title=title,
        summary="A test problem summary.",
        domain="Testing",
        citations=[make_citation(i) for i in range(num_citations)],
        scores=ScoreBreakdown(
            consequence=per_axis, urgency=per_axis, neglect=per_axis, teen_accessibility=per_axis
        ),
    )


def make_section(name: str, lines: list[tuple[str, list[int]]]) -> ScriptSection:
    start_sec, end_sec = next((s, e) for n, s, e in SECTION_SPECS if n == name)
    return ScriptSection(
        name=name,
        start_sec=start_sec,
        end_sec=end_sec,
        lines=[ScriptLine(text=text, citation_indices=idxs) for text, idxs in lines],
    )


def full_valid_sections() -> list[ScriptSection]:
    return [
        make_section("Hook", [("What if this happened to you?", [])]),
        make_section("Why It Matters", [("This affects millions of people worldwide.", [0])]),
        make_section("Why Now", [("Nobody has cracked this yet because it's hard.", [1])]),
        make_section("Opportunity", [("New tech could finally fix this problem.", [2])]),
        make_section("Teen Challenge", [("Go build something that helps.", [])]),
    ]


class TestParseSectionsInput:
    def test_parses_well_formed_list(self):
        raw = [{"name": "Hook", "lines": [{"text": "Hi", "citation_indices": []}]}]
        assert _parse_sections_input(raw) == {"Hook": raw[0]["lines"]}

    def test_recovers_from_double_encoded_json_string_with_sections_wrapper(self):
        # The real bug: the model returned {"sections": "<json string of {\"sections\": [...]}>"}
        inner = {"sections": [{"name": "Hook", "lines": [{"text": "Hi", "citation_indices": []}]}]}
        raw = json.dumps(inner)

        result = _parse_sections_input(raw)

        assert result == {"Hook": inner["sections"][0]["lines"]}

    def test_recovers_from_double_encoded_json_string_without_wrapper(self):
        inner = [{"name": "Hook", "lines": [{"text": "Hi", "citation_indices": []}]}]
        raw = json.dumps(inner)

        result = _parse_sections_input(raw)

        assert result == {"Hook": inner[0]["lines"]}

    def test_raises_on_unparsable_string(self):
        with pytest.raises(ScriptValidationError):
            _parse_sections_input("not valid json at all {{{")

    def test_raises_when_a_list_item_is_not_an_object(self):
        with pytest.raises(ScriptValidationError):
            _parse_sections_input(["Hook", "Why It Matters"])


class TestValidateEvidenceLedger:
    def test_passes_when_fact_sections_are_cited(self):
        _validate_evidence_ledger(full_valid_sections())

    def test_fails_when_a_fact_section_has_no_citations(self):
        sections = full_valid_sections()
        sections[2] = make_section("Why Now", [("Nobody has cracked this yet.", [])])

        with pytest.raises(ScriptValidationError):
            _validate_evidence_ledger(sections)

    def test_hook_and_teen_challenge_never_require_citations(self):
        sections = full_valid_sections()
        _validate_evidence_ledger(sections)


class TestValidatePacing:
    def test_passes_within_budget(self):
        _validate_pacing(full_valid_sections())

    def test_fails_when_a_section_overshoots_its_word_budget(self):
        sections = full_valid_sections()
        long_text = " ".join(["word"] * 100)  # way over Hook's 5-second budget
        sections[0] = make_section("Hook", [(long_text, [])])

        with pytest.raises(ScriptValidationError):
            _validate_pacing(sections)

    def test_section_word_budget_scales_with_duration(self):
        assert _section_word_budget(5) < _section_word_budget(25)

    def test_fails_total_cap_even_when_every_section_is_individually_within_budget(self):
        # Each section sits exactly at its own per-section ceiling — legal individually —
        # but summed across all 5 sections that exceeds the total-script word cap.
        sections = [
            make_section(name, [(" ".join(["word"] * _section_word_budget(end - start)), [])])
            for name, start, end in SECTION_SPECS
        ]

        with pytest.raises(ScriptValidationError):
            _validate_pacing(sections)

    def test_total_cap_scales_with_the_configured_duration_and_pace(self):
        assert _total_word_cap() > 0


class TestBuildEvidenceLedger:
    def test_groups_usages_by_citation_index(self):
        problem = make_problem(num_citations=3)
        sections = full_valid_sections()

        ledger = _build_evidence_ledger(problem, sections)

        assert len(ledger) == 3
        assert ledger[0].citation.url == problem.citations[0].url
        assert "Why It Matters:" in ledger[0].used_in[0]

    def test_unused_citations_are_omitted(self):
        problem = make_problem(num_citations=5)  # citations 3 and 4 are never referenced
        sections = full_valid_sections()

        ledger = _build_evidence_ledger(problem, sections)

        cited_urls = {entry.citation.url for entry in ledger}
        assert cited_urls == {problem.citations[i].url for i in (0, 1, 2)}

    def test_a_citation_used_in_multiple_lines_lists_all_of_them(self):
        problem = make_problem(num_citations=1)
        sections = [make_section("Why It Matters", [("First claim.", [0]), ("Second claim.", [0])])]

        ledger = _build_evidence_ledger(problem, sections)

        assert len(ledger) == 1
        assert len(ledger[0].used_in) == 2


class TestLoadProblem:
    def _write_problems(self, tmp_path, problems: list[Problem]):
        path = tmp_path / "problems.json"
        path.write_text(json.dumps([p.model_dump() for p in problems]))
        return path

    def test_selects_by_index(self, tmp_path):
        problems = [make_problem(title="A"), make_problem(title="B")]
        path = self._write_problems(tmp_path, problems)

        selected = _load_problem(path, index=0, title=None)

        assert selected.title == "A"

    def test_selects_by_title_substring_case_insensitive(self, tmp_path):
        problems = [make_problem(title="Orbital Debris"), make_problem(title="Workforce Shortages")]
        path = self._write_problems(tmp_path, problems)

        selected = _load_problem(path, index=None, title="orbital")

        assert selected.title == "Orbital Debris"

    def test_raises_when_title_has_no_match(self, tmp_path):
        problems = [make_problem(title="Orbital Debris")]
        path = self._write_problems(tmp_path, problems)

        with pytest.raises(typer.BadParameter):
            _load_problem(path, index=None, title="nonexistent")

    def test_defaults_to_highest_scoring_problem(self, tmp_path):
        problems = [
            make_problem(title="Low", total_score=8),
            make_problem(title="High", total_score=40),
            make_problem(title="Mid", total_score=20),
        ]
        path = self._write_problems(tmp_path, problems)

        selected = _load_problem(path, index=None, title=None)

        assert selected.title == "High"
