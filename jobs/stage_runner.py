"""Generic stage runner (one Lakeflow Job for every stage of every run).

Started by the app with --run-id, --stage and --action:
  draft   : the stage agent drafts the artifact + review packet; status -> awaiting_review
  execute : the executor applies an approved artifact in dev schemas; status -> done

Stage 0 is the Phase 0 smoke test: records a stage run, writes one artifact to the Volume,
opens an MLflow run, and finishes.
Stages 1-8 are looked up in STAGES and implemented in agents/stages/ (Phase 1 onward).
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
import traceback
import uuid
from datetime import datetime, timezone

# Make the repo root importable when run as a spark_python_task. Serverless runs the file via
# exec(compile(source, filename)), so __file__ is undefined; the code object still has the filename.
import inspect  # noqa: E402

_THIS_FILE = globals().get("__file__") or inspect.currentframe().f_code.co_filename
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(_THIS_FILE), "..")))

import mlflow  # noqa: E402
from databricks import sql as dbsql  # noqa: E402
from databricks.sdk.core import Config  # noqa: E402

STAGES = {
    0: None,  # smoke test
    1: "agents.stages.s1_profile",
    2: "agents.stages.s2_enrich",
    3: "agents.stages.s3_glossary",
    4: "agents.stages.s4_model",
    5: "agents.stages.s5_metric_views",
    6: "agents.stages.s6_genie_assignment",
    7: "agents.stages.s7_genie_build",
    8: "agents.stages.s8_benchmarks",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--stage", type=int, required=True)
    p.add_argument("--action", choices=["draft", "execute"], default="draft")
    p.add_argument("--catalog", required=True)
    p.add_argument("--warehouse-id", required=True)
    p.add_argument("--llm-endpoint", required=True)
    p.add_argument("--secret-scope", required=True)
    p.add_argument("--git-repo", required=True)
    p.add_argument("--git-branch", default="main")
    p.add_argument("--source-id", default="_smoke", help="Looked up from control.runs once runs exist (Phase 1)")
    p.add_argument("--job-run-id", default="")
    p.add_argument("--experiment-dir", default="/Shared/semantic_builder_poc")
    return p.parse_args()


class ControlStore:
    """Minimal writer for control.stage_runs (append a row per status change; latest row wins)."""

    def __init__(self, catalog: str, warehouse_id: str) -> None:
        self.table = f"{catalog}.control.stage_runs"
        cfg = Config()
        self._conn = dbsql.connect(
            server_hostname=cfg.host.replace("https://", ""),
            http_path=f"/sql/1.0/warehouses/{warehouse_id}",
            credentials_provider=lambda: cfg.authenticate,
        )

    def record(self, *, stage_run_id: str, run_id: str, stage: int, attempt: int, status: str,
               artifact_path: str | None = None, job_run_id: int | None = None,
               trace_id: str | None = None, error_message: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        with self._conn.cursor() as cur:
            cur.execute(
                f"MERGE INTO {self.table} t USING (SELECT :stage_run_id AS stage_run_id) s "
                "ON t.stage_run_id = s.stage_run_id "
                "WHEN MATCHED THEN UPDATE SET status = :status, artifact_path = coalesce(:artifact_path, t.artifact_path), "
                "trace_id = coalesce(:trace_id, t.trace_id), error_message = :error_message, updated_at = :now "
                "WHEN NOT MATCHED THEN INSERT (stage_run_id, run_id, stage, attempt, status, artifact_path, job_run_id, "
                "trace_id, error_message, started_at, updated_at) VALUES (:stage_run_id, :run_id, :stage, :attempt, "
                ":status, :artifact_path, :job_run_id, :trace_id, :error_message, :now, :now)",
                {"stage_run_id": stage_run_id, "run_id": run_id, "stage": stage, "attempt": attempt,
                 "status": status, "artifact_path": artifact_path, "job_run_id": job_run_id,
                 "trace_id": trace_id, "error_message": error_message, "now": now},
            )

    def next_attempt(self, run_id: str, stage: int) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT coalesce(max(attempt), 0) + 1 FROM {self.table} WHERE run_id = :run_id AND stage = :stage",
                        {"run_id": run_id, "stage": stage})
            return int(cur.fetchone()[0])


def main() -> None:
    args = parse_args()
    if args.stage not in STAGES:
        raise SystemExit(f"unknown stage {args.stage}")
    from databricks.sdk import WorkspaceClient

    WorkspaceClient().workspace.mkdirs(args.experiment_dir)  # set_experiment needs the parent folder
    mlflow.set_tracking_uri("databricks")
    mlflow.set_experiment(f"{args.experiment_dir}/stage_agents")

    store = ControlStore(args.catalog, args.warehouse_id)
    attempt = store.next_attempt(args.run_id, args.stage)
    stage_run_id = f"{args.run_id}-s{args.stage}-a{attempt}-{uuid.uuid4().hex[:6]}"
    job_run_id = int(args.job_run_id) if args.job_run_id.strip().isdigit() else None
    running = "drafting" if args.action == "draft" else "executing"
    store.record(stage_run_id=stage_run_id, run_id=args.run_id, stage=args.stage, attempt=attempt,
                 status=running, job_run_id=job_run_id)

    try:
        with mlflow.start_run(run_name=stage_run_id):
            if STAGES[args.stage] is None:
                from tools.artifact_writer import ArtifactWriter

                writer = ArtifactWriter(catalog=args.catalog, source_id=args.source_id, run_id=args.run_id,
                                        stage=0, attempt=attempt)
                path = writer.write_json("smoke.json", {"stage_run_id": stage_run_id, "job_run_id": job_run_id,
                                                         "at": datetime.now(timezone.utc).isoformat()})
                mlflow.log_param("artifact_path", path)
                result = {"status": "done", "artifact_path": path}
            else:
                module = importlib.import_module(STAGES[args.stage])
                result = getattr(module, args.action)(args=args, stage_run_id=stage_run_id)
            trace_id = mlflow.get_last_active_trace_id()
        store.record(stage_run_id=stage_run_id, run_id=args.run_id, stage=args.stage, attempt=attempt,
                     status=result["status"], artifact_path=result.get("artifact_path"), trace_id=trace_id)
    except Exception as e:
        store.record(stage_run_id=stage_run_id, run_id=args.run_id, stage=args.stage, attempt=attempt,
                     status="failed", error_message=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-3000:]}")
        raise


if __name__ == "__main__":
    main()
