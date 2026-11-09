# Build Plan: Agentic Semantic Layer Builder (POC)

Companion to `design_doc.md`. Status: Phase 0 complete; Phase 1 next.

## Confirmed decisions

| Topic | Decision |
|---|---|
| LLM | `databricks-claude-sonnet-5-5` via Foundation Model APIs, small custom tool-calling loop, MLflow tracing |
| App | Streamlit on Databricks Apps |
| Agent execution | One generic stage runner job (`run_id`, `stage` parameters), started by the app; app polls control tables |
| Facts vs. reasoning | Deterministic Python computes facts and checks; LLM interprets, proposes options, writes packets |
| Control data | Delta tables in `semantic_builder_poc.control`, accessed through the SQL warehouse |
| Artifacts | Volume `semantic_builder_poc.artifacts.files` + Git commits via GitHub REST API (repo-scoped token in secret scope `semantic_builder_poc`) |
| Identity | One service principal runs the app and jobs, owns the catalog; read-only on raw schemas |
| Silver and gold code | SQL in one Lakeflow Spark Declarative Pipeline per source, dev schemas only |
| Writes | Create-only; never overwrite or drop; metric views change via `ALTER VIEW` |

## Repo layout

```
databricks.yml              bundle: app, jobs, variables
resources/                  app.yml, jobs.yml
setup/                      00_setup.sql (catalog, schemas, volume), 01_control_tables.sql
app/                        Streamlit app: pages, controller, validator, renderer, control-table access
agents/                     common/ (llm, tools, packet), stages/ (s1_profile ... s8_benchmarks)
agents/prompts/             one markdown prompt per stage (no source-specific content)
tools/                      uc_metadata, readonly_sql, artifact_writer, git_commit, library, glossary, genie_api
templates/                  pipeline SQL, metric view YAML, Genie config
jobs/                       stage_runner.py, executor.py, benchmark_runner.py
config/sources/             example registration YAML
eval/                       held-out answer key per test dataset (never read by agents or prompts)
tests/                      unit tests
```

## Phase 0: Foundations (about 1 week)

1. Bundle skeleton (`databricks.yml`, app and job resources).
2. Service principal; catalog, schemas (`control`, `artifacts`), Volume; grants (read-only on raw).
3. Control tables (9, see design doc section 10).
4. Review packet schema (pydantic): the contract between agents, validator and renderer.
5. Shared code: LLM client with tracing, read-only SQL guard, config loader, artifact writer, Git commit helper.
6. Secret scope with the GitHub token.

Exit: bundle deploys; empty app opens; stage runner job runs a no-op stage; SP can read the raw schema and cannot write to it.

Status (2026-11-09), verified by `setup/phase0_setup_and_verify`:

| Item | Status |
|---|---|
| Catalog, schemas, Volume, 9 control tables | Done (created, 9 PKs + 4 FKs) |
| Packet schema, config, read-only guard, artifact writer | Done (31 unit tests pass; overwrite blocked) |
| Warehouse connection | Done |
| Stage runner stage 0 (control row + Volume file + MLflow) | Done as a deployed job (run 823683923469979) |
| LLM tool loop on sonnet-5-5 with tracing | Done (model rejects `temperature`; omitted by default) |
| Secret scope `semantic_builder_poc` + `github_token` | Done; token can push to aryanshreni/Enrich_Agent |
| `databricks bundle validate --strict -t dev` | OK |
| Job `stage_runner` deployed | Done (source-linked) |
| App `sb-poc-senthil-raja` | Running (deployed from the UI; resources sql-warehouse, job, model) |
| App SP grants | Done and verified (raw read-only) |
| Job `run_as` app SP | Done; stage 0 job run 6106644954212 succeeded as the SP |
| Smoke test from the app | Done: run smoke-6bc883e8, job run 754843670092995 started by the app SP, status done |

**Phase 0 complete (2026-11-09).**

## Phase 1: Skeleton + Stages 1-3 on NetSuite (about 3 weeks)

- Controller (state machine), validator, renderer (both views), decision logging, gate summary.
- UI: registration, run tracker, review cards, view toggle, bulk approval, defer, reject.
- Stage 1 Profile, Stage 2 Enrich (silver pipeline + reconciliation), Stage 3 Glossary (provided and generated).

Exit: NetSuite through gate 3; agents independently surface the known issues in `eval/netsuite` (duplicate customer ids, 0 sentinel keys, orphan task project ids, HH.MM hours column).

## Phase 2: Stages 4-8 + chat + clarity (about 4 weeks)

- Stage 4 model (grain, orphan and reconciliation checks; conformed dimension proposals).
- Stage 5 metric views (YAML aliases off, full nested join paths, dimension names differ from join aliases, create once then ALTER).
- Stage 6 assignment (overlap score: glossary terms, column similarity, shared dimensions).
- Stage 7 Genie build (GET, merge, PATCH; never replace content owned by other sources).
- Stage 8 benchmarks (expected SQL answers only what was asked; eval-runs API; fixes loop through Stage 7).
- Chat panel and clarity feedback.

Exit: NetSuite Genie agent at 90% or higher benchmark pass rate.

## Phase 3: Keka, no code changes (about 2 weeks)

Exit: Keka completes all eight stages; Stage 6 proposes combining with the NetSuite agent; effort, clarity, reuse and quality captured.

## Still open

See design doc section 17 (clarity target, redraft limit, overlap threshold).
