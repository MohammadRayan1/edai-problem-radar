from __future__ import annotations

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError

from radar.models import Citation, Problem, ScoreBreakdown
from radar.research_engine import (
    TAVILY_MAX_RETRIES,
    _enforce_citation_diversity,
    _has_distinct_citation_sources,
    _search_with_retry,
)


def make_problem(title: str = "Test Problem", urls: list[str] | None = None) -> Problem:
    urls = urls if urls is not None else ["https://a.example.com", "https://b.example.com"]
    return Problem(
        title=title,
        summary="A test problem summary.",
        domain="Testing",
        citations=[Citation(url=u, quote=f"quote from {u}") for u in urls],
        scores=ScoreBreakdown(consequence=5, urgency=5, neglect=5, teen_accessibility=5),
    )


class TestHasDistinctCitationSources:
    def test_true_for_two_different_urls(self):
        problem = make_problem(urls=["https://a.example.com", "https://b.example.com"])
        assert _has_distinct_citation_sources(problem) is True

    def test_false_for_the_same_url_repeated(self):
        problem = make_problem(urls=["https://a.example.com", "https://a.example.com"])
        assert _has_distinct_citation_sources(problem) is False

    def test_false_for_a_single_citation(self):
        problem = make_problem(urls=["https://a.example.com"])
        assert _has_distinct_citation_sources(problem) is False

    def test_true_when_more_than_two_urls_are_all_distinct(self):
        problem = make_problem(
            urls=["https://a.example.com", "https://b.example.com", "https://c.example.com"]
        )
        assert _has_distinct_citation_sources(problem) is True


class TestEnforceCitationDiversity:
    def test_keeps_problems_with_distinct_source_citations(self):
        problems = [make_problem("Good", urls=["https://a.example.com", "https://b.example.com"])]

        kept = _enforce_citation_diversity(problems)

        assert [p.title for p in kept] == ["Good"]

    def test_drops_problems_whose_citations_share_one_url(self):
        problems = [make_problem("Bad", urls=["https://a.example.com", "https://a.example.com"])]

        kept = _enforce_citation_diversity(problems)

        assert kept == []

    def test_mixed_batch_keeps_only_the_compliant_ones(self):
        problems = [
            make_problem("Good", urls=["https://a.example.com", "https://b.example.com"]),
            make_problem("Bad", urls=["https://a.example.com", "https://a.example.com"]),
            make_problem("AlsoGood", urls=["https://c.example.com", "https://d.example.com"]),
        ]

        kept = _enforce_citation_diversity(problems)

        assert [p.title for p in kept] == ["Good", "AlsoGood"]


class FakeTavilyClient:
    def __init__(self, side_effects: list):
        self._side_effects = list(side_effects)
        self.calls = 0

    def search(self, **kwargs):
        self.calls += 1
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


class TestSearchWithRetry:
    def test_returns_the_result_on_first_success(self, monkeypatch):
        monkeypatch.setattr("radar.research_engine.time.sleep", lambda _: None)
        client = FakeTavilyClient([{"results": ["ok"]}])

        result = _search_with_retry(client, "some query")

        assert result == {"results": ["ok"]}
        assert client.calls == 1

    def test_retries_after_a_timeout_and_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("radar.research_engine.time.sleep", lambda _: None)
        client = FakeTavilyClient([TimeoutError("read timed out"), {"results": ["ok"]}])

        result = _search_with_retry(client, "some query")

        assert result == {"results": ["ok"]}
        assert client.calls == 2

    def test_retries_after_a_requests_connection_error(self, monkeypatch):
        monkeypatch.setattr("radar.research_engine.time.sleep", lambda _: None)
        client = FakeTavilyClient([RequestsConnectionError("connection reset"), {"results": ["ok"]}])

        result = _search_with_retry(client, "some query")

        assert result == {"results": ["ok"]}
        assert client.calls == 2

    def test_raises_after_exhausting_all_retries(self, monkeypatch):
        monkeypatch.setattr("radar.research_engine.time.sleep", lambda _: None)
        client = FakeTavilyClient([TimeoutError("still timing out")] * TAVILY_MAX_RETRIES)

        with pytest.raises(TimeoutError):
            _search_with_retry(client, "some query")

        assert client.calls == TAVILY_MAX_RETRIES
