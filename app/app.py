"""Semantic Layer Builder app (Phase 0 shell).

Phase 0 scope: prove the app can reach the control tables through the SQL warehouse and
start the stage runner job (stage 0 smoke test). Registration, run tracker, gate summaries
and review cards are added in Phase 1.
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from databricks import sql as dbsql  # noqa: E402
from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.core import Config  # noqa: E402

st.set_page_config(page_title="Semantic Layer Builder", layout="wide")

CATALOG = os.environ.get("SB_CATALOG", "semantic_builder_poc")
_missing = [v for v in ("SB_WAREHOUSE_ID", "SB_STAGE_RUNNER_JOB_ID") if not os.environ.get(v)]
if _missing:
    st.error(f"Missing environment variables {_missing}. Check that the app resources are named "
             "sql-warehouse and job, matching app.yaml.")
    st.stop()
WAREHOUSE_ID = os.environ["SB_WAREHOUSE_ID"]
JOB_ID = int(os.environ["SB_STAGE_RUNNER_JOB_ID"])


@st.cache_resource
def _cfg() -> Config:
    return Config()


def query(sql: str, params: dict | None = None) -> pd.DataFrame:
    cfg = _cfg()
    with dbsql.connect(
        server_hostname=cfg.host.replace("https://", ""),
        http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
        credentials_provider=lambda: cfg.authenticate,
    ) as conn, conn.cursor() as cur:
        cur.execute(sql, params or {})
        cols = [d[0] for d in cur.description or []]
        return pd.DataFrame(cur.fetchall(), columns=cols)


st.title("Semantic Layer Builder (POC)")
st.caption("raw → silver → glossary → gold → metric views → Genie agent → benchmarks")

tab_sources, tab_runs, tab_health = st.tabs(["Sources", "Runs", "System check"])

with tab_sources:
    st.info("Source registration arrives in Phase 1.")
    st.dataframe(query(f"SELECT source_id, raw_catalog, raw_schema, registered_at FROM {CATALOG}.control.sources "
                       "ORDER BY registered_at DESC"), use_container_width=True)

with tab_runs:
    st.dataframe(query(f"SELECT run_id, stage, attempt, status, updated_at, error_message FROM {CATALOG}.control.stage_runs "
                       "ORDER BY updated_at DESC LIMIT 50"), use_container_width=True)

with tab_health:
    st.write("Starts the stage runner job with stage 0 (no-op). It should appear on the Runs tab as done.")
    if st.button("Run smoke test"):
        run_id = f"smoke-{uuid.uuid4().hex[:8]}"
        w = WorkspaceClient()
        w.jobs.run_now(job_id=JOB_ID, job_parameters={"run_id": run_id, "stage": "0", "action": "draft"})
        st.success(f"Started stage runner for {run_id}.")
