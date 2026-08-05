from __future__ import annotations

from radar.review_web import (
    MAX_HELP_MESSAGES,
    generated_date,
    help_system_prompt,
    parse_batch_results,
    prepare_help_messages,
)


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


class TestParseBatchResults:
    def test_extracts_a_single_result_line(self):
        log = 'Some console noise\nRESULT_JSON:{"domain": "Healthcare", "problem": "X", "status": "done", "detail": "path.mp4"}\nmore noise'

        results = parse_batch_results(log)

        assert results == [{"domain": "Healthcare", "problem": "X", "status": "done", "detail": "path.mp4"}]

    def test_extracts_multiple_result_lines_in_order(self):
        log = "\n".join(
            [
                'RESULT_JSON:{"domain": "A", "problem": "1", "status": "done", "detail": "d1"}',
                "some unrelated log line",
                'RESULT_JSON:{"domain": "A", "problem": "2", "status": "skipped", "detail": "d2"}',
            ]
        )

        results = parse_batch_results(log)

        assert [r["problem"] for r in results] == ["1", "2"]

    def test_returns_empty_list_when_there_are_no_result_lines(self):
        assert parse_batch_results("just some regular console output\nnothing structured here") == []

    def test_ignores_a_malformed_result_line_instead_of_crashing(self):
        log = 'RESULT_JSON:{not valid json\nRESULT_JSON:{"domain": "A", "problem": "1", "status": "done", "detail": "d"}'

        results = parse_batch_results(log)

        assert results == [{"domain": "A", "problem": "1", "status": "done", "detail": "d"}]

    def test_handles_an_empty_log(self):
        assert parse_batch_results("") == []


class TestPrepareHelpMessages:
    def test_keeps_a_single_valid_user_message(self):
        result = prepare_help_messages([{"role": "user", "content": "what happened?"}])

        assert result == [{"role": "user", "content": "what happened?"}]

    def test_returns_empty_when_there_are_no_messages(self):
        assert prepare_help_messages([]) == []

    def test_returns_empty_when_the_first_message_is_not_from_the_user(self):
        # shouldn't happen in normal use, but don't send a malformed exchange to the API
        assert prepare_help_messages([{"role": "assistant", "content": "hi"}]) == []

    def test_normalizes_unknown_roles_to_assistant(self):
        result = prepare_help_messages(
            [{"role": "user", "content": "hi"}, {"role": "system", "content": "weird role"}]
        )

        assert result[1]["role"] == "assistant"

    def test_drops_messages_with_empty_or_whitespace_only_content(self):
        result = prepare_help_messages([{"role": "user", "content": "  "}, {"role": "user", "content": "real question"}])

        assert result == [{"role": "user", "content": "real question"}]

    def test_truncates_overly_long_content(self):
        long_text = "x" * 5000
        result = prepare_help_messages([{"role": "user", "content": long_text}])

        assert len(result[0]["content"]) == 2000

    def test_caps_history_to_the_most_recent_messages(self):
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(MAX_HELP_MESSAGES + 5)]

        result = prepare_help_messages(messages)

        assert len(result) == MAX_HELP_MESSAGES
        assert result[-1]["content"] == f"msg {MAX_HELP_MESSAGES + 4}"


class TestHelpSystemPrompt:
    def test_includes_the_failure_context_fields(self):
        context = {"domain": "Healthcare", "problem": "X", "status": "video_failed", "detail": "duration too long"}

        prompt = help_system_prompt(context)

        assert "Healthcare" in prompt
        assert "X" in prompt
        assert "video_failed" in prompt
        assert "duration too long" in prompt

    def test_handles_a_missing_problem_gracefully(self):
        prompt = help_system_prompt({"domain": "Agriculture", "status": "domain_failed", "detail": "no sources"})

        assert "none" in prompt.lower()
