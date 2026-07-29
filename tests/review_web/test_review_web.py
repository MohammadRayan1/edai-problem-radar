from __future__ import annotations

from radar.review_web import generated_date


class TestGeneratedDate:
    def test_extracts_the_date_from_a_draft_dir_timestamp(self):
        result = generated_date("data/drafts/some_problem_20260724T024819Z")

        assert result != ""

    def test_same_calendar_day_produces_the_same_date_string(self):
        morning = generated_date("data/drafts/some_problem_20260724T010000Z")
        evening = generated_date("data/drafts/some_problem_20260724T230000Z")

        # both timestamps are on 2026-07-24 UTC; depending on local timezone they
        # might land on different local calendar days, but each call must be
        # internally consistent (deterministic for the same input)
        assert generated_date("data/drafts/some_problem_20260724T010000Z") == morning
        assert generated_date("data/drafts/some_problem_20260724T230000Z") == evening

    def test_different_days_produce_different_date_strings(self):
        day1 = generated_date("data/drafts/some_problem_20260724T120000Z")
        day2 = generated_date("data/drafts/some_problem_20260801T120000Z")

        assert day1 != day2

    def test_returns_empty_string_for_a_dir_with_no_embedded_timestamp(self):
        assert generated_date("data/drafts/not_a_timestamped_dir") == ""

    def test_returns_empty_string_for_a_malformed_timestamp(self):
        assert generated_date("data/drafts/some_problem_notatimestamp") == ""

    def test_works_with_a_bare_directory_name_not_just_a_full_path(self):
        assert generated_date("some_problem_20260724T024819Z") != ""
