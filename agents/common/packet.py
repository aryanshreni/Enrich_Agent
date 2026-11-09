"""Review packet schema: the contract between stage agents, the packet validator and the card renderer.

Every stage agent returns one ReviewPacket per gate, holding one ReviewCard per decision
(design doc 7.1: one decision per card). Validation rules come from design doc 7.7:
  - every required section present in both views
  - two to four options, exactly one recommended
  - evidence rows come from real queries (query_id + sql recorded)
  - technical terms used in the business view are defined
  - no SQL in the business view
Low-risk items go in bulk_items instead of full cards (design doc section 6).
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class RiskTier(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Confidence(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class CheckStatus(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


# Heuristic for SQL leaking into business-view text.
_SQL_PATTERN = re.compile(
    r"\b(select\s+.+\s+from|insert\s+into|update\s+\w+\s+set|delete\s+from|create\s+(or\s+replace\s+)?(table|view)|"
    r"group\s+by|order\s+by|left\s+join|inner\s+join|where\s+\w+\s*(=|<|>|in\b|is\b))",
    re.IGNORECASE | re.DOTALL,
)


def contains_sql(text: str | None) -> bool:
    return bool(text) and bool(_SQL_PATTERN.search(text))


class Evidence(BaseModel):
    """What the agent found. Rows must come from a real query that was executed."""

    narrative: str = Field(..., description="Business view: counts and patterns in plain language")
    query_id: str = Field(..., description="ID of the executed read-only query (links to the trace)")
    sql: str = Field(..., description="Technical view only: the query that produced the rows")
    columns: list[str]
    rows: list[list[Any]] = Field(..., min_length=1, max_length=10)
    total_matching_rows: int | None = None
    statistics: dict[str, Any] = Field(default_factory=dict, description="Technical view: profiling stats")


class Option(BaseModel):
    option_id: str = Field(..., pattern=r"^[A-D]$")
    label: str
    what_it_means: str
    pros: list[str] = Field(..., min_length=1)
    cons: list[str] = Field(..., min_length=1)
    what_happens_next: str
    generated_code: str | None = Field(None, description="Technical view: code applied if chosen")
    payload: dict[str, Any] = Field(default_factory=dict, description="Structured content the executor applies")


class Recommendation(BaseModel):
    option_id: str = Field(..., pattern=r"^[A-D]$")
    reason: str
    confidence: Confidence


class Check(BaseModel):
    name: str
    status: CheckStatus
    explanation: str = Field(..., description="One line, business view")
    sql: str | None = Field(None, description="Technical view")
    result: Any = None


class PriorDecision(BaseModel):
    library_entry_id: str
    approved_on: str
    summary: str
    source_id: str


class ViewText(BaseModel):
    """A section rendered in both views. Technical text adds to, never replaces, the business text."""

    business: str
    technical: str


class ReviewCard(BaseModel):
    card_key: str = Field(..., description="Stable key within the stage, e.g. 'dedup:accounts'")
    risk_tier: RiskTier
    title: str = Field(..., description="The decision, phrased as a question")
    summary: ViewText
    why_it_matters: ViewText
    affected_objects: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(..., min_length=1)
    options: list[Option] = Field(..., min_length=2, max_length=4)
    recommendation: Recommendation
    checks: list[Check] = Field(default_factory=list)
    previously_approved: list[PriorDecision] = Field(default_factory=list)
    term_definitions: dict[str, str] = Field(
        default_factory=dict, description="Tooltip text for technical terms used in the business view"
    )
    depends_on: list[str] = Field(default_factory=list, description="card_keys that must be decided first")

    @field_validator("title")
    @classmethod
    def _title_is_question(cls, v: str) -> str:
        if not v.strip().endswith("?"):
            raise ValueError("title must be phrased as a question")
        return v

    @model_validator(mode="after")
    def _validate(self) -> "ReviewCard":
        ids = [o.option_id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("option ids must be unique")
        if self.recommendation.option_id not in ids:
            raise ValueError("recommendation must point to one of the options")
        business_texts = [self.title, self.summary.business, self.why_it_matters.business]
        business_texts += [e.narrative for e in self.evidence]
        business_texts += [c.explanation for c in self.checks]
        for o in self.options:
            business_texts += [o.label, o.what_it_means, o.what_happens_next, *o.pros, *o.cons]
        business_texts.append(self.recommendation.reason)
        for t in business_texts:
            if contains_sql(t):
                raise ValueError(f"business view contains SQL: {t[:80]!r}")
        return self


class BulkItem(BaseModel):
    """Low-risk item shown on the gate summary bulk-approval list."""

    item_key: str
    description: str
    reason: str = Field(..., description="Short reason shown on the line")
    payload: dict[str, Any] = Field(default_factory=dict)


class ReviewPacket(BaseModel):
    run_id: str
    stage: int = Field(..., ge=1, le=8)
    stage_summary: str = Field(..., description="What this stage does, one or two sentences")
    artifact_path: str
    cards: list[ReviewCard] = Field(default_factory=list)
    bulk_items: list[BulkItem] = Field(default_factory=list)
    unlocks: str = Field(..., description="What becomes available once this gate is approved")

    @model_validator(mode="after")
    def _validate(self) -> "ReviewPacket":
        keys = [c.card_key for c in self.cards]
        if len(set(keys)) != len(keys):
            raise ValueError("card keys must be unique within a packet")
        for c in self.cards:
            if c.risk_tier == RiskTier.LOW:
                raise ValueError(f"low-risk item {c.card_key} must be a bulk item, not a card")
            missing = [d for d in c.depends_on if d not in keys]
            if missing:
                raise ValueError(f"card {c.card_key} depends on unknown cards {missing}")
        return self

    def estimated_review_minutes(self) -> int:
        per_tier = {RiskTier.HIGH: 4, RiskTier.MEDIUM: 2}
        return sum(per_tier[c.risk_tier] for c in self.cards) + max(1, len(self.bulk_items) // 10)


def validate_packet(data: dict[str, Any] | str) -> ReviewPacket:
    """Validate raw agent output. Raises pydantic.ValidationError with every problem found."""
    if isinstance(data, str):
        return ReviewPacket.model_validate_json(data)
    return ReviewPacket.model_validate(data)
