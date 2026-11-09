-- Control tables (design doc section 10). Create-only: IF NOT EXISTS, nothing dropped or replaced.
-- Status values:
--   runs.status, stage_runs.status: pending | drafting | awaiting_review | approved | rejected | executing | done | failed | stale
--   review_cards.status: open | deferred | decided | superseded
--   risk_tier: high | medium | low

CREATE TABLE IF NOT EXISTS semantic_builder_poc.control.sources (
  source_id        STRING    NOT NULL COMMENT 'User-defined source identifier',
  raw_catalog      STRING    NOT NULL,
  raw_schema       STRING    NOT NULL,
  target_catalog   STRING    NOT NULL,
  silver_schema    STRING    NOT NULL,
  gold_schema      STRING    NOT NULL,
  description      STRING,
  domain_label     STRING    COMMENT 'Optional tag, used only for decision library matching',
  config_yaml      STRING    NOT NULL COMMENT 'Full registration config as submitted',
  registered_by    STRING    NOT NULL,
  registered_at    TIMESTAMP NOT NULL,
  CONSTRAINT sources_pk PRIMARY KEY (source_id)
) COMMENT 'Registered sources and their config';

CREATE TABLE IF NOT EXISTS semantic_builder_poc.control.runs (
  run_id           STRING    NOT NULL,
  source_id        STRING    NOT NULL,
  status           STRING    NOT NULL,
  current_stage    INT       COMMENT '1-8',
  started_by       STRING    NOT NULL,
  started_at       TIMESTAMP NOT NULL,
  updated_at       TIMESTAMP NOT NULL,
  finished_at      TIMESTAMP,
  CONSTRAINT runs_pk PRIMARY KEY (run_id),
  CONSTRAINT runs_source_fk FOREIGN KEY (source_id) REFERENCES semantic_builder_poc.control.sources (source_id)
) COMMENT 'One row per run, with status and timestamps';

CREATE TABLE IF NOT EXISTS semantic_builder_poc.control.stage_runs (
  stage_run_id     STRING    NOT NULL,
  run_id           STRING    NOT NULL,
  stage            INT       NOT NULL COMMENT '1 Profile, 2 Enrich, 3 Glossary, 4 Model, 5 Metric views, 6 Genie assignment, 7 Genie build, 8 Benchmarks',
  attempt          INT       NOT NULL COMMENT 'Increments on reject-and-redraft or rerun',
  status           STRING    NOT NULL,
  artifact_path    STRING    COMMENT 'Volume path of the artifact version',
  artifact_version STRING    COMMENT 'Git commit SHA once committed',
  job_run_id       BIGINT    COMMENT 'Stage runner job run',
  trace_id         STRING    COMMENT 'MLflow trace of the agent invocation',
  error_message    STRING,
  started_at       TIMESTAMP NOT NULL,
  updated_at       TIMESTAMP NOT NULL,
  CONSTRAINT stage_runs_pk PRIMARY KEY (stage_run_id),
  CONSTRAINT stage_runs_run_fk FOREIGN KEY (run_id) REFERENCES semantic_builder_poc.control.runs (run_id)
) COMMENT 'Status per stage per run, the artifact version, and the agent trace ID';

CREATE TABLE IF NOT EXISTS semantic_builder_poc.control.review_cards (
  card_id          STRING    NOT NULL,
  stage_run_id     STRING    NOT NULL,
  run_id           STRING    NOT NULL,
  stage            INT       NOT NULL,
  risk_tier        STRING    NOT NULL,
  title            STRING    NOT NULL,
  packet_json      STRING    NOT NULL COMMENT 'Validated review packet (both views)',
  depends_on       ARRAY<STRING> COMMENT 'card_ids that must be decided first',
  status           STRING    NOT NULL,
  created_at       TIMESTAMP NOT NULL,
  updated_at       TIMESTAMP NOT NULL,
  CONSTRAINT review_cards_pk PRIMARY KEY (card_id),
  CONSTRAINT review_cards_stage_run_fk FOREIGN KEY (stage_run_id) REFERENCES semantic_builder_poc.control.stage_runs (stage_run_id)
) COMMENT 'Rendered packets with risk tier and status';

CREATE TABLE IF NOT EXISTS semantic_builder_poc.control.review_decisions (
  decision_id      STRING    NOT NULL,
  card_id          STRING    NOT NULL,
  reviewer         STRING    NOT NULL,
  reviewer_role    STRING    COMMENT 'data_engineer | data_owner | analyst',
  view_used        STRING    NOT NULL COMMENT 'business | technical',
  action           STRING    NOT NULL COMMENT 'choose | edit | defer | reject | bulk_approve',
  option_id        STRING,
  edits_json       STRING    COMMENT 'Reviewer edits to the chosen option',
  reason           STRING    COMMENT 'Required for reject',
  clarity_rating   BOOLEAN   COMMENT 'Was this clear? yes/no',
  clarity_comment  STRING,
  artifact_version STRING,
  trace_id         STRING,
  decided_at       TIMESTAMP NOT NULL,
  CONSTRAINT review_decisions_pk PRIMARY KEY (decision_id),
  CONSTRAINT review_decisions_card_fk FOREIGN KEY (card_id) REFERENCES semantic_builder_poc.control.review_cards (card_id)
) COMMENT 'Reviewer, view used, option chosen, edits, reason, timestamp, clarity rating';

CREATE TABLE IF NOT EXISTS semantic_builder_poc.control.decision_library (
  entry_id         STRING    NOT NULL,
  entry_type       STRING    NOT NULL COMMENT 'convention | metric_definition | join_pattern | genie_assignment | rejected_proposal | correction',
  outcome          STRING    NOT NULL COMMENT 'approved | rejected',
  source_id        STRING    NOT NULL,
  domain_label     STRING,
  summary          STRING    NOT NULL COMMENT 'Plain-language statement of the decision',
  payload_json     STRING    NOT NULL COMMENT 'Structured decision content',
  match_metadata   STRING    COMMENT 'Table/column metadata and glossary terms used for similarity matching (JSON)',
  reason           STRING,
  decision_id      STRING    COMMENT 'Originating review decision',
  created_at       TIMESTAMP NOT NULL,
  CONSTRAINT decision_library_pk PRIMARY KEY (entry_id)
) COMMENT 'Reusable approved and rejected decisions with metadata used for matching';

CREATE TABLE IF NOT EXISTS semantic_builder_poc.control.glossary (
  term_id          STRING    NOT NULL,
  source_id        STRING    NOT NULL,
  term             STRING    NOT NULL,
  definition       STRING    NOT NULL,
  synonyms         ARRAY<STRING>,
  column_mappings  ARRAY<STRUCT<table_name: STRING, column_name: STRING>>,
  is_conceptual    BOOLEAN   NOT NULL COMMENT 'True when the term intentionally has no column mapping',
  origin           STRING    NOT NULL COMMENT 'provided | generated',
  approval_status  STRING    NOT NULL COMMENT 'draft | approved | rejected',
  run_id           STRING,
  updated_at       TIMESTAMP NOT NULL,
  CONSTRAINT glossary_pk PRIMARY KEY (term_id)
) COMMENT 'Terms, definitions, synonyms, column mappings, origin, approval status';

CREATE TABLE IF NOT EXISTS semantic_builder_poc.control.genie_assignments (
  assignment_id    STRING    NOT NULL,
  source_id        STRING    NOT NULL,
  genie_space_id   STRING    COMMENT 'Set once the agent exists',
  genie_agent_name STRING    NOT NULL,
  mode             STRING    NOT NULL COMMENT 'new | combined | split',
  overlap_score    DOUBLE,
  overlap_rationale STRING,
  included_assets  ARRAY<STRING> COMMENT 'Fully qualified metric views and tables',
  audience         STRING,
  run_id           STRING    NOT NULL,
  approved_at      TIMESTAMP,
  CONSTRAINT genie_assignments_pk PRIMARY KEY (assignment_id)
) COMMENT 'Mapping of sources to Genie agents, and the overlap rationale';

CREATE TABLE IF NOT EXISTS semantic_builder_poc.control.benchmark_results (
  result_id        STRING    NOT NULL,
  run_id           STRING    NOT NULL,
  genie_space_id   STRING    NOT NULL,
  eval_run_id      STRING    COMMENT 'Genie evaluation run',
  question         STRING    NOT NULL,
  expected_sql     STRING    NOT NULL,
  generated_sql    STRING,
  passed           BOOLEAN,
  source_ids       ARRAY<STRING> COMMENT 'Sources the question spans',
  failure_diagnosis STRING,
  executed_at      TIMESTAMP NOT NULL,
  CONSTRAINT benchmark_results_pk PRIMARY KEY (result_id)
) COMMENT 'Question, expected SQL, generated SQL, pass/fail, run ID';
