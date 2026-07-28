from __future__ import annotations

import json

import pytest
import typer
from sqlmodel import Session, select

from radar.review_cli import CITATION_STRETCH_THRESHOLD, _decide, _evidence_warnings, _get_record, _sync_drafts
from radar.storage import ReviewRecord, get_engine


def make_draft(drafts_dir, name: str, **overrides) -> None:
    draft_dir = drafts_dir / name
    draft_dir.mkdir(parents=True)
    meta = {
        "problem_title": overrides.get("problem_title", "Test Problem"),
        "domain": overrides.get("domain", "Testing"),
        "video_path": str(draft_dir / "draft.mp4"),
        "script_path": str(draft_dir / "script.json"),
        "total_duration_seconds": overrides.get("total_duration_seconds", 45.0),
    }
    (draft_dir / "meta.json").write_text(json.dumps(meta))


class TestSyncDrafts:
    def test_creates_a_record_per_draft_with_meta_json(self, tmp_path):
        drafts_dir = tmp_path / "drafts"
        make_draft(drafts_dir, "problem_a")
        make_draft(drafts_dir, "problem_b")

        engine = get_engine(tmp_path / "test.db")
        with Session(engine) as session:
            _sync_drafts(session, drafts_dir)
            records = session.exec(select(ReviewRecord)).all()

        assert len(records) == 2
        assert all(r.status == "pending" for r in records)

    def test_skips_directories_without_meta_json(self, tmp_path):
        drafts_dir = tmp_path / "drafts"
        drafts_dir.mkdir()
        (drafts_dir / "not_a_draft").mkdir()

        engine = get_engine(tmp_path / "test.db")
        with Session(engine) as session:
            _sync_drafts(session, drafts_dir)
            records = session.exec(select(ReviewRecord)).all()

        assert records == []

    def test_does_not_duplicate_on_repeated_sync(self, tmp_path):
        drafts_dir = tmp_path / "drafts"
        make_draft(drafts_dir, "problem_a")

        engine = get_engine(tmp_path / "test.db")
        with Session(engine) as session:
            _sync_drafts(session, drafts_dir)
            _sync_drafts(session, drafts_dir)
            records = session.exec(select(ReviewRecord)).all()

        assert len(records) == 1

    def test_missing_drafts_dir_is_a_noop(self, tmp_path):
        engine = get_engine(tmp_path / "test.db")
        with Session(engine) as session:
            _sync_drafts(session, tmp_path / "does_not_exist")
            records = session.exec(select(ReviewRecord)).all()

        assert records == []


class TestDecide:
    def _seed_one(self, tmp_path):
        drafts_dir = tmp_path / "drafts"
        make_draft(drafts_dir, "problem_a")
        db_path = tmp_path / "test.db"

        engine = get_engine(db_path)
        with Session(engine) as session:
            _sync_drafts(session, drafts_dir)
        return db_path

    def test_approve_sets_status_and_clears_notes(self, tmp_path):
        db_path = self._seed_one(tmp_path)

        _decide(1, "approved", None, db_path)

        engine = get_engine(db_path)
        with Session(engine) as session:
            record = session.get(ReviewRecord, 1)
        assert record.status == "approved"
        assert record.notes is None
        assert record.decided_at is not None

    def test_reject_stores_the_note(self, tmp_path):
        db_path = self._seed_one(tmp_path)

        _decide(1, "rejected", "too long", db_path)

        engine = get_engine(db_path)
        with Session(engine) as session:
            record = session.get(ReviewRecord, 1)
        assert record.status == "rejected"
        assert record.notes == "too long"


class TestGetRecord:
    def test_raises_typer_exit_for_a_missing_id(self, tmp_path):
        engine = get_engine(tmp_path / "test.db")
        with Session(engine) as session:
            with pytest.raises(typer.Exit):
                _get_record(session, 999)


def make_ledger_entry(used_in_count: int) -> dict:
    return {
        "citation": {"url": "https://example.com", "quote": "some quote"},
        "used_in": [f"line {i}" for i in range(used_in_count)],
    }


class TestEvidenceWarnings:
    def test_no_warnings_when_every_citation_is_used_within_threshold(self):
        script_data = {
            "evidence_ledger": [
                make_ledger_entry(1),
                make_ledger_entry(CITATION_STRETCH_THRESHOLD),
            ]
        }

        assert _evidence_warnings(script_data) == []

    def test_flags_a_citation_reused_beyond_the_threshold(self):
        script_data = {"evidence_ledger": [make_ledger_entry(CITATION_STRETCH_THRESHOLD + 1)]}

        warnings = _evidence_warnings(script_data)

        assert len(warnings) == 1
        assert "[0]" in warnings[0]

    def test_flags_each_stretched_citation_independently(self):
        script_data = {
            "evidence_ledger": [
                make_ledger_entry(1),
                make_ledger_entry(CITATION_STRETCH_THRESHOLD + 2),
                make_ledger_entry(CITATION_STRETCH_THRESHOLD + 5),
            ]
        }

        warnings = _evidence_warnings(script_data)

        assert len(warnings) == 2

    def test_empty_ledger_has_no_warnings(self):
        assert _evidence_warnings({"evidence_ledger": []}) == []
