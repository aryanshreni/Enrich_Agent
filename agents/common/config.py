"""Source registration config (design doc section 4). Nothing here is domain-specific."""
from __future__ import annotations

import fnmatch
import re
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(v: str) -> str:
    if not _IDENT.match(v):
        raise ValueError(f"invalid identifier: {v!r}")
    return v


class RawLocation(BaseModel):
    catalog: str
    schema_: str = Field(..., alias="schema")

    @field_validator("catalog", "schema_")
    @classmethod
    def _check_ident(cls, v: str) -> str:
        return _ident(v)

    @property
    def fqn(self) -> str:
        return f"{self.catalog}.{self.schema_}"


class TargetLocation(BaseModel):
    catalog: str
    silver_schema: str
    gold_schema: str

    @field_validator("catalog", "silver_schema", "gold_schema")
    @classmethod
    def _check_ident(cls, v: str) -> str:
        return _ident(v)


class Scope(BaseModel):
    include_tables: list[str] = Field(default_factory=lambda: ["*"])
    exclude_tables: list[str] = Field(default_factory=list)

    def selects(self, table_name: str) -> bool:
        inc = any(fnmatch.fnmatch(table_name, p) for p in self.include_tables)
        exc = any(fnmatch.fnmatch(table_name, p) for p in self.exclude_tables)
        return inc and not exc


class GlossaryInput(BaseModel):
    source: Literal["upload", "table", "none"] = "none"
    location: str | None = None

    @model_validator(mode="after")
    def _loc(self) -> "GlossaryInput":
        if self.source != "none" and not self.location:
            raise ValueError("glossary.location is required when glossary.source is upload or table")
        return self


class Reviewers(BaseModel):
    data_engineer: str
    data_owner: str
    analyst: str


class SourceConfig(BaseModel):
    source_id: str
    raw_location: RawLocation
    target_location: TargetLocation
    description: str
    domain_label: str | None = None
    scope: Scope = Field(default_factory=Scope)
    seed_questions: list[str] = Field(default_factory=list)
    glossary: GlossaryInput = Field(default_factory=GlossaryInput)
    reviewers: Reviewers

    @field_validator("source_id")
    @classmethod
    def _check_ident(cls, v: str) -> str:
        return _ident(v)

    @model_validator(mode="after")
    def _not_raw(self) -> "SourceConfig":
        t = self.target_location
        if (t.catalog, t.silver_schema) == (self.raw_location.catalog, self.raw_location.schema_) or (
            t.catalog, t.gold_schema) == (self.raw_location.catalog, self.raw_location.schema_):
            raise ValueError("target schemas must differ from the raw schema (raw is read-only)")
        return self


def load_source_config(text: str) -> SourceConfig:
    return SourceConfig.model_validate(yaml.safe_load(text))
