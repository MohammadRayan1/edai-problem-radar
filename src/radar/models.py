from __future__ import annotations

from pydantic import BaseModel, Field


class Citation(BaseModel):
    url: str
    quote: str
    source_title: str | None = None


class ScoreBreakdown(BaseModel):
    consequence: int = Field(ge=1, le=10)
    urgency: int = Field(ge=1, le=10)
    neglect: int = Field(ge=1, le=10)
    teen_accessibility: int = Field(ge=1, le=10)

    @property
    def total(self) -> int:
        return self.consequence + self.urgency + self.neglect + self.teen_accessibility


class Problem(BaseModel):
    title: str
    summary: str
    domain: str
    citations: list[Citation]
    scores: ScoreBreakdown
    score_rationale: str | None = None

    @property
    def total_score(self) -> int:
        return self.scores.total


class ScriptLine(BaseModel):
    text: str
    citation_indices: list[int] = Field(default_factory=list)


class ScriptSection(BaseModel):
    name: str
    start_sec: int
    end_sec: int
    lines: list[ScriptLine]


class EvidenceLedgerEntry(BaseModel):
    citation: Citation
    used_in: list[str]


class Script(BaseModel):
    problem_title: str
    domain: str
    source_path: str
    sections: list[ScriptSection]
    evidence_ledger: list[EvidenceLedgerEntry]

    @property
    def total_est_seconds(self) -> int:
        return max((s.end_sec for s in self.sections), default=0)
