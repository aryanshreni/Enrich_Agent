"""Unity Catalog metadata reads for stage agents (read-only, via information_schema)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents.common.config import _ident
from tools.readonly_sql import ReadOnlySql


@dataclass
class UcMetadata:
    sql: ReadOnlySql

    def list_tables(self, catalog: str, schema: str) -> list[dict[str, Any]]:
        c, s = _ident(catalog), _ident(schema)
        out = self.sql.run(
            f"SELECT table_name, table_type, comment FROM `{c}`.information_schema.tables "
            f"WHERE table_schema = '{s}' ORDER BY table_name")
        return [dict(zip(out["columns"], r)) for r in out["rows"]]

    def list_columns(self, catalog: str, schema: str, table: str) -> list[dict[str, Any]]:
        c, s, t = _ident(catalog), _ident(schema), _ident(table)
        out = self.sql.run(
            f"SELECT column_name, data_type, is_nullable, comment, ordinal_position "
            f"FROM `{c}`.information_schema.columns WHERE table_schema = '{s}' AND table_name = '{t}' "
            f"ORDER BY ordinal_position")
        return [dict(zip(out["columns"], r)) for r in out["rows"]]
