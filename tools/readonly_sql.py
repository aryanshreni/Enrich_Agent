"""Read-only SQL tool for stage agents.

Agents may only read. Every statement is parsed and rejected unless it is a single
SELECT / WITH / UNION query, or DESCRIBE / SHOW. Results are capped, and each executed
query gets a query_id so review-card evidence can be traced back to a real query.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Describe)
_FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.TruncateTable, exp.Grant, exp.Command, exp.Set, exp.Use,
)


class ReadOnlyViolation(ValueError):
    pass


def assert_read_only(statement: str) -> str:
    text = statement.strip().rstrip(";").strip()
    if not text:
        raise ReadOnlyViolation("empty statement")
    upper = text.upper()
    if upper.startswith(("SHOW ", "DESCRIBE ", "DESC ")):
        if ";" in text:
            raise ReadOnlyViolation("only one statement is allowed")
        return text
    try:
        parsed = sqlglot.parse(text, read="databricks")
    except sqlglot.errors.ParseError as e:
        raise ReadOnlyViolation(f"could not parse statement: {e}") from e
    if len(parsed) != 1 or parsed[0] is None:
        raise ReadOnlyViolation("only one statement is allowed")
    root = parsed[0]
    if not isinstance(root, _ALLOWED_ROOTS):
        raise ReadOnlyViolation(f"statement type {type(root).__name__} is not allowed")
    bad = next(root.find_all(*_FORBIDDEN_NODES), None)
    if bad is not None:
        raise ReadOnlyViolation(f"statement contains {type(bad).__name__}")
    return text


@dataclass
class QueryLog:
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ReadOnlySql:
    warehouse_id: str
    max_rows: int = 200
    log: QueryLog = field(default_factory=QueryLog)

    def _connect(self):
        # Imported lazily so the guard can be used and tested without the connector installed.
        from databricks import sql as dbsql
        from databricks.sdk.core import Config

        cfg = Config()
        return dbsql.connect(
            server_hostname=cfg.host.replace("https://", ""),
            http_path=f"/sql/1.0/warehouses/{self.warehouse_id}",
            credentials_provider=lambda: cfg.authenticate,
        )

    def run(self, sql: str) -> dict[str, Any]:
        """Tool entry point. Returns query_id, columns, rows (capped) and whether output was truncated."""
        text = assert_read_only(sql)
        query_id = hashlib.md5(f"{text}|{time.time_ns()}".encode()).hexdigest()[:12]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(text)
            rows = cur.fetchmany(self.max_rows + 1)
            columns = [d[0] for d in cur.description or []]
        truncated = len(rows) > self.max_rows
        rows = [list(r) for r in rows[: self.max_rows]]
        self.log.entries[query_id] = {"sql": text, "row_count": len(rows), "truncated": truncated}
        return {"query_id": query_id, "columns": columns, "rows": rows, "truncated": truncated}

    def was_executed(self, query_id: str, sql: str) -> bool:
        """Used by the packet validator: evidence must cite a query that actually ran."""
        entry = self.log.entries.get(query_id)
        return entry is not None and entry["sql"].strip() == sql.strip().rstrip(";").strip()
