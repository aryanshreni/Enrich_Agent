-- Phase 0 setup: catalog, schemas and artifact Volume for the Semantic Layer Builder POC.
-- Create-only: every statement uses IF NOT EXISTS; nothing is dropped or replaced.
-- Run once on the SQL warehouse, then run 01_control_tables.sql.

CREATE CATALOG IF NOT EXISTS semantic_builder_poc
  COMMENT 'Agentic Semantic Layer Builder POC: control tables, artifacts, and per-source silver and gold dev schemas';

CREATE SCHEMA IF NOT EXISTS semantic_builder_poc.control
  COMMENT 'Run state, review cards and decisions, decision library, glossary, Genie assignments, benchmark results';

CREATE SCHEMA IF NOT EXISTS semantic_builder_poc.artifacts
  COMMENT 'Working copy of generated artifacts (profiles, pipeline code, model specs, metric view YAML, Genie configs, benchmarks, review packets)';

CREATE VOLUME IF NOT EXISTS semantic_builder_poc.artifacts.files
  COMMENT 'Artifacts organized as /<source_id>/<run_id>/<stage>/';

-- Grants for the app service principal (replace <app_sp_application_id>, keep the backticks).
-- Owns the dev catalog; read-only on each registered raw schema.
-- ALTER CATALOG semantic_builder_poc OWNER TO `<app_sp_application_id>`;
-- GRANT USE CATALOG ON CATALOG <raw_catalog> TO `<app_sp_application_id>`;
-- GRANT USE SCHEMA, SELECT ON SCHEMA <raw_catalog>.<raw_schema> TO `<app_sp_application_id>`;
