from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Field, SQLModel, create_engine


class ReviewRecord(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    draft_dir: str = Field(unique=True, index=True)
    problem_title: str
    domain: str
    video_path: str
    meta_path: str
    script_path: str
    status: str = "pending"  # pending | approved | rejected | changes_requested
    notes: str | None = None
    total_duration_seconds: float = 0.0
    decided_at: str | None = None


def get_engine(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    return engine


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
