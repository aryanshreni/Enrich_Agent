# Design Document: Agentic Semantic Layer Builder (POC)

**Status:** Draft, proof of concept  
**Delivery:** Databricks App  
**Environment:** One dev workspace (no production target)  
**Scope:** Works for any domain

---

## 1. Purpose

Prove that one repeatable, agent-driven process with human review can take any raw schema in Unity Catalog through these layers:

```
raw data → enriched (silver) → business glossary → facts and dimensions (gold) → metric views → Genie agent → benchmarks
```

The process is domain-agnostic. Domain knowledge comes only from the data itself, the user's inputs (including an optional glossary), and decisions reviewers approve during the run. At every gate, both business and technical reviewers get clear, plain-language explanations and concrete options, so they can decide confidently.

---

## 2. Goals and Non-Goals

### Goals
- A single Databricks App where a user registers a raw schema and is guided through each stage.
- Agents draft every artifact; a person approves, edits or rejects it at each gate.
- Every gate gives the reviewer ample explanation, real data examples, and two to four options with trade-offs and a recommendation, in both a business view and a technical view.
- No domain-specific logic in code. Everything specific to a source lives in config, the glossary, or approved decisions.
- Show that the process works on two unrelated test datasets without code changes.
- Collect evidence on feasibility at scale: review effort, proposal acceptance, explanation clarity, and benchmark quality.

### Non-Goals (POC)
- Production deployment, promotion between environments, CI/CD, or SLAs.
- Autonomous deployment without review.
- Ingestion from source systems. The process starts from raw tables already in Unity Catalog.
- Streaming or real-time processing.
- Multi-tenant access control beyond standard Unity Catalog permissions.

---

## 3. Architecture Overview

```
┌─────────────────────────── Databricks App ───────────────────────────┐
│ UI: Source registration · Run tracker · Gate summaries · Review cards │
│     Business / Technical view toggle · "Ask the agent" chat · Diffs   │
│     Bulk approval                                                     │
│ Backend: stage controller (state machine) · packet validator ·        │
│          card renderer · artifact writer (Volume + Git)               │
└───────────────┬──────────────────────────────────┬───────────────────┘
                │                                  │
     Stage agents (Foundation Model APIs)  Execution (Jobs / SQL Warehouse)
     1 Profile                             - Pipeline runs in dev schemas
     2 Enrich                              - Read-only validation queries
     3 Business glossary                   - Metric view DDL
     4 Model                               - Genie agent API calls
     5 Metric views                        - Benchmark runs
     6 Genie assignment
     7 Genie build
     8 Benchmarks
     Each agent outputs: artifact + review packet
                │                                  │
        ┌───────┴──────────────────────────────────┴───────┐
        │ Control tables (Delta): runs, stages, cards,     │
        │   decisions, decision library, glossary,         │
        │   benchmark results                              │
        │ Artifacts: UC Volume (working copy) + Git (history)│
        │ Traces: MLflow (linked to review decisions)      │
        └──────────────────────────────────────────────────┘
```

### Components
- **Databricks App:** the single entry point. It handles source registration, run tracking, gate summary pages, review cards with a business/technical view toggle, a chat panel on each card, diffs, and bulk approval.
- **Stage controller:** a state machine per run (`pending → drafting → awaiting_review → approved/rejected → executing → done`). A stage advances only after its gate is approved.
- **Stage agents:** one agent per stage, run by a generic stage runner job (a Lakeflow Job started by the app with `run_id` and `stage` as parameters) using the `databricks-claude-sonnet-5-5` Foundation Model API endpoint. Drafting takes minutes, so it never runs inside an app request; the app polls the control tables for status. Plain Python computes the facts (uniqueness, NULL rates, key overlap, reconciliations); the LLM interprets them, proposes options and writes the review packet. Their tools are Unity Catalog metadata reads, read-only SQL, artifact writes, and lookups in the decision library and glossary. Each produces an artifact plus a structured review packet. Agents never write data directly; execution happens only after approval.
- **Packet validator and card renderer:** check that each review packet is complete and readable, then render it as review cards in both views.
- **Execution layer:** Lakeflow Jobs, Lakeflow Spark Declarative Pipelines and a SQL warehouse run approved artifacts in dev schemas only. Writes are create-only: never overwrite or drop an existing table or view; changes to metric views use `ALTER VIEW`.
- **Control tables:** all run state, review decisions, the glossary, the decision library and benchmark results live in Delta tables (section 10).
- **Artifacts:** written to a Unity Catalog Volume (the working copy the app reads) and committed to Git (version history and review of code changes). Commits are made by the stage runner through the GitHub REST API, using a fine-grained token (write access to this repo only) stored in a secret scope that the app and job service principal can read. This lets a run go end to end across a schema without any interactive user session.
- **Observability:** an MLflow trace for every agent invocation, linked to the review decision on it.

---

## 4. Source Registration (User Input)

Registered through a guided form in the app and stored in the control tables:

```yaml
source_id: <user-defined>
raw_location: { catalog: <catalog>, schema: <schema> }
target_location: { catalog: <dev_catalog>, silver_schema: <name>, gold_schema: <name> }
description: <free-text description of the data and its purpose>
domain_label: <optional free-text tag, used only for decision library matching>
scope:
  include_tables: [<patterns>]
  exclude_tables: [<patterns>]
seed_questions:            # optional; agents propose if empty
  - <question>
glossary:                  # optional; if omitted, the workflow generates one (Stage 3)
  source: <upload file | existing table | none>
  location: <path or table name, if provided>
reviewers:
  data_engineer: <user/group>
  data_owner: <user/group>
  analyst: <user/group>
```

### Glossary Handling
- **Provided:** the glossary is loaded at registration. Stage 3 reconciles it with the data (terms with no matching data, data with no term, conflicting definitions) and asks reviewers to resolve the gaps.
- **Not provided:** the user skips this field. Stage 3 generates a draft glossary from the profile, the enrichment decisions and column semantics, and sends it for review.

---

## 5. Stage Specifications

Each stage is described by its inputs, what the agent does, its outputs, its automated checks, and its reviewers. Section 7.4 gives the typical decision cards for each gate.

### Stage 1: Profile
- **Inputs:** registration config, raw tables.
- **Agent actions:** read schema and comments; sample data; work out candidate keys, grain, event vs. load timestamps, enum encodings, units, sign conventions, NULL rates and relationships (key-overlap analysis); flag likely PII.
- **Outputs:** a profile file per table; an inferred relationship graph; a review packet.
- **Automated checks:** candidate keys are unique; foreign-key matches are above a threshold.
- **Reviewers:** data engineer.

### Stage 2: Enrich (Silver)
- **Inputs:** approved profile.
- **Agent actions:** propose type casts, decoded values, deduplication, lookup joins, naming standards, and table and column descriptions.
- **Outputs:** pipeline code, data quality rules (expectations), description files, a review packet.
- **Automated checks:** a dev pipeline run succeeds; row counts reconcile with raw (allowing for documented dedup or filtering); the expectations pass.
- **Reviewers:** data engineer plus data owner.

### Stage 3: Business Glossary
- **Inputs:** the glossary provided at registration (if any), the approved profile, silver tables and descriptions.
- **Agent actions:**
  - If a glossary was provided: map each term to tables and columns; flag terms with no matching data, important columns with no term, and conflicting definitions.
  - If none was provided: generate terms, plain-language definitions, synonyms and mappings to tables and columns, from the data and approved decisions.
- **Outputs:** a glossary in the control table, a review packet.
- **Automated checks:** every term maps to at least one column or is marked conceptual; no duplicate terms; key columns are covered.
- **Reviewers:** data owner plus analyst.
- **Downstream use:** model naming, metric definitions, Genie agent instructions and synonyms, and the wording of review cards.

### Stage 4: Facts and Dimensions (Gold)
- **Inputs:** silver tables, the approved glossary, seed questions, and the gold dimensions already built for other sources.
- **Agent actions:** propose fact tables with an explicit grain statement, the dimensions they link to, and how dimension history is tracked (SCD type); plus surrogate keys, join paths and date handling. Names follow glossary terms. When an existing gold dimension from another source describes the same entity (measured by key overlap and glossary match), propose reusing it as a shared (conformed) dimension instead of building a duplicate; the reviewer decides.
- **Outputs:** a model spec file, a relationship diagram, pipeline code, a review packet.
- **Automated checks:** fact grain is unique; every fact row links to a dimension row (orphan rate under a threshold); additive measures reconcile with silver.
- **Reviewers:** data engineer plus data owner. **This is the most critical gate.**

### Stage 5: Metric Views
- **Inputs:** gold model, glossary, seed questions, decision library.
- **Agent actions:** draft measures, dimensions and default filters; check each measure with SQL for signs, units, percentage format and double counting; reuse approved definitions where they apply.
- **Outputs:** metric view files, glossary entries for each measure, a review packet.
- **Automated checks:** each measure is compared with an independent query on gold.
- **Reviewers:** data owner (mandatory).

### Stage 6: Genie Agent Assignment
- **Inputs:** approved metric views, gold tables, glossary, the list of existing Genie agents in dev and their assigned sources.
- **Agent actions:** measure overlap with existing agents (shared terms, related dimensions, similar questions). When there's meaningful overlap, the default proposal is a combined agent; otherwise a new one. Also propose which metric views and tables to include, the agent's name and description, and its intended audience.
- **Outputs:** an assignment proposal (the mapping from source to Genie agent), a review packet.
- **Automated checks:** the combined agent stays within table limits; no conflicting measure names across combined sources; no duplicate assignments.
- **Reviewers:** data owner plus analyst.

### Stage 7: Genie Agent Build
- **Inputs:** approved assignment, glossary, metric views.
- **Agent actions:** configure value indexing, column visibility, joins, measures, filters, example SQL, text instructions, synonyms from the glossary, and starter questions. For combined agents, merge into the existing configuration without duplicating or conflicting with it.
- **Outputs:** a Genie agent config file (applied through the API in dev), a review packet.
- **Automated checks:** no duplicate instructions; no SQL inside text instructions; every example SQL runs.
- **Reviewers:** analyst.

### Stage 8: Benchmarks
- **Inputs:** the Genie agent, glossary, seed questions.
- **Agent actions:** generate benchmark questions with expected SQL across measures, dimensions, filters, glossary synonyms and edge cases. For combined agents, add questions that span sources. Run the benchmarks, diagnose failures, and propose targeted fixes, which go back through the Stage 7 gate.
- **Outputs:** a benchmark set, a run report, a review packet.
- **Automated checks:** the pass rate is at least 90%; minimum coverage for each measure.
- **Reviewers:** data owner plus analyst.

---

## 6. Risk Tiers

| Tier | Examples | Review Mode |
|---|---|---|
| High | Grain or key changes, metric definitions, PII handling, glossary definitions of key terms, Genie agent assignment, benchmark expected answers | Individual card, explicit choice required |
| Medium | New tables, join changes, dedup and filter rules, hidden columns, glossary synonyms | Individual card, batch review allowed |
| Low | Descriptions, value indexing, starter questions | Bulk-approval list, each line with a short reason |

---

## 7. Human Review Design

### 7.1 Principles

- **Two views on every card.** The business view is the default: plain language, glossary terms, impact on reports. The technical view is one click away: SQL, YAML, schemas, diffs, check details. Both show the same options and recommendation.
- **One decision per card.** A gate with several decisions shows several cards.
- **Always offer options.** Each decision has two to four concrete options, each with what it means, pros, cons and what happens next. A reviewer is never asked to write a solution from scratch.
- **Recommend, but don't pre-select.** The agent marks its recommended option, says why, and gives its confidence (High / Medium / Low). The reviewer still makes an explicit choice.
- **Show real examples.** Explain abstract issues with a few actual rows from the data.
- **Explain the impact.** Every card says what goes wrong downstream if the decision is wrong.
- **Define terms where they appear.** Technical terms get an inline tooltip. Business terms from the approved glossary are used once Stage 3 is complete.
- **Keep the layout consistent.** Every card at every gate has the same structure.

### 7.2 Review Card Structure (Standard for All Gates)

| Section | Business View | Technical View |
|---|---|---|
| Title | The decision, phrased as a question | Same |
| Summary | 2–3 plain-language sentences | Adds affected objects and operations |
| Why it matters | Effect on reports and answers | Adds affected downstream artifacts |
| What the agent found | Counts, patterns, 5–10 sample rows, a simple chart | Adds profiling statistics and the queries used |
| Options | 2–4 choices: what it means, pros, cons, what happens next | Adds the generated code for each option |
| Recommendation | Option, reason, confidence | Same |
| Checks | Passed / Warning / Failed, with a one-line explanation | Adds check SQL and results |
| Previously approved | "A similar decision was approved on [date]: …" | Adds the library entry ID and source |
| Actions | Choose, edit, ask the agent, defer, reject | Same, plus editing the code directly |

### 7.3 Reviewer Actions

- **Choose an option.** This applies the selected option.
- **Edit.** Adjust the chosen option, either in a form (business view) or in the code (technical view). The edit is saved to the decision library as a correction.
- **Ask the agent.** A chat panel on each card for follow-up questions. The answer appears on the card, and the decision stays open.
- **Defer.** Park the card and continue with independent cards. Stages that depend on it stay blocked.
- **Reject.** Requires a short reason. The agent redrafts with that feedback. After N attempts, the item is handled manually.

### 7.4 Decisions by Gate

All options are generated from the data. The ones below are typical patterns, not fixed lists.

| Gate | Typical Decision Cards | Example Options Offered |
|---|---|---|
| 1. Profile | Which tables are in scope? What uniquely identifies a record? Which date is the "event" date? Is this column personal data? | Include / exclude / include later; Key A vs. key B vs. combined key; Created vs. updated vs. other date |
| 2. Enrich | What do these codes mean? How to handle duplicates? What to do with incomplete records? | Keep latest / keep first / keep all with history; Exclude / keep and flag / fill default |
| 3. Glossary | Is this definition correct? Which column represents this term? Are these synonyms? This term has no matching data: keep or drop? | Accept / reword / reject; Column A vs. column B; Merge / keep separate |
| 4. Model | What does one row in this fact table represent? Which descriptive tables connect to it? Should history be kept? | One row per event vs. per line item; Keep history vs. current only |
| 5. Metric views | How should this measure be calculated? Which records count? What's the default time period? | Include vs. exclude record types; Sum vs. distinct count vs. average |
| 6. Genie agent assignment | Combine with an existing agent, or create a new one? Which assets should it include? Who is the audience? | Combine with agent X (default when overlapping) / new agent / split |
| 7. Genie agent build | Which columns should be hidden? Is this instruction clear? Are these starter questions right? | Hide / show with explanation / keep; Accept / reword / remove |
| 8. Benchmarks | Is this expected answer correct? Is coverage good enough to finish the run? | Correct / incorrect (with explanation) / unsure, route to another reviewer; Sign off / add more |

### 7.5 Example Review Card (Business View, Illustrative)

```
How should duplicate records be handled in the "accounts" table?

Summary
I found 412 records (3.1%) that share the same account ID. Before
building reports, we need one rule for which version to keep.

Why it matters
If duplicates are kept, counts and totals for accounts will be
overstated by about 3%.

What I found
account_id | name          | status   | updated_at
1042       | Acme Ltd      | active   | 2024-03-01
1042       | Acme Limited  | active   | 2024-06-15
1088       | Northwind     | inactive | 2023-11-20
1088       | Northwind     | active   | 2024-01-05
(showing 4 of 412 — view more)

Options
A. Keep the most recently updated record      [Recommended · High confidence]
   Reports reflect the latest information. Older versions are dropped.
B. Keep the first record ever created
   Preserves original values; recent changes are ignored.
C. Keep all versions and track history
   Allows "as of" questions; adds complexity to the model in Stage 4.

Why I recommend A
Every duplicate has a later updated_at and the newer value appears
to be a correction.

Checks: Passed: key unique after rule A · Warning: 6 duplicates have identical timestamps

[Choose A] [Choose B] [Choose C] [Edit] [Ask the agent] [Defer] [Reject]
[Switch to technical view]
```

### 7.6 Gate Summary Page

Each gate opens with a one-screen overview:

- What this stage does, in one or two sentences.
- The number of decisions waiting, by risk tier.
- An estimated review time.
- The low-risk bulk-approval list, each line with a short reason.
- Progress through all eight stages, and what unlocks once this gate is approved.

### 7.7 Explanation Quality Controls

- Each stage agent outputs a structured review packet (business and technical content for summary, impact, evidence, options, recommendation, checks) along with its artifact. The app renders this packet; it doesn't generate explanations on the fly.
- Before a card is shown, the validator checks the packet. It must:
  - include every required section in both views;
  - offer between two and four options;
  - have evidence rows that come from real queries;
  - define any technical terms used in the business view;
  - keep SQL out of the business view.
- Each card asks the reviewer "Was this clear?" (yes / no plus an optional comment). The target is 90% rated clear. That feedback is used to improve the packet prompts.
- Every decision is recorded with the reviewer, view used, a timestamp, the chosen option, any edits, the artifact version and the linked agent trace.

---

## 8. Decision Library

- **Contents:** approved conventions (date handling, signs, units, code mappings), metric definitions, join patterns, Genie agent assignments, and rejected proposals with their reasons.
- **Scope:** entries are tagged by source, and optionally by domain label. Matching is by similarity of table and column metadata and glossary terms, not hard-coded domain rules.
- **How it's used:** agents look up relevant entries before drafting. Matches are labeled "previously approved" on review cards.
- **Purpose in the POC:** measure whether reuse lowers review effort on the second test dataset.

---

## 9. Change Handling (POC Scope)

- A user can rerun from any stage. Downstream stages are marked stale and must pass review again.
- Schema-drift detection is a manual "re-profile" action in the POC. Automatic triggers are deferred.
- Glossary changes flag the affected metric views, Genie agent instructions and benchmarks for review.
- Benchmarks rerun automatically after any change in Stages 5 to 7.

---

## 10. Data Storage

### Control Tables (Delta, in a `control` schema)

| Table | Purpose |
|---|---|
| `sources` | Registered sources and their config |
| `runs` | One row per run, with status and timestamps |
| `stage_runs` | Status per stage per run, the artifact version, and the agent trace ID |
| `review_cards` | Rendered packets with risk tier and status |
| `review_decisions` | Reviewer, view used, option chosen, edits, reason, timestamp, clarity rating |
| `decision_library` | Reusable approved and rejected decisions with metadata used for matching |
| `glossary` | Terms, definitions, synonyms, column mappings, origin (provided or generated), approval status |
| `genie_assignments` | Mapping of sources to Genie agents, and the overlap rationale |
| `benchmark_results` | Question, expected SQL, generated SQL, pass/fail, run ID |

### Artifacts
- **Unity Catalog Volume:** the working copy of profiles, pipeline code, model specs, metric view YAML, Genie agent configs, benchmark sets and review packets, organized as `/<source_id>/<run_id>/<stage>/`.
- **Git:** every approved artifact is committed with a message that references the run and review decision IDs.

---

## 11. Environment and Security

- Single dev workspace with a dedicated dev catalog for control tables, artifacts, and silver and gold output.
- The app runs as a service principal with read access to registered raw schemas, and write access only to the dev catalog and dev Genie agents.
- Columns flagged as PII are excluded or masked by default unless a reviewer approves otherwise.
- Production promotion, CI/CD and rollback beyond artifact versioning are out of scope.

---

## 12. Where It Lives in the Workspace

| Component | Location |
|---|---|
| Code (app, agents, prompts, pipeline templates) | A Git folder, deployed with Declarative Automation Bundles |
| App | Databricks Apps, deployed from the Git folder |
| Control tables | A dev catalog, e.g. `semantic_builder_poc.control` |
| Artifacts | `semantic_builder_poc.artifacts` Volume, plus Git |
| Silver and gold output | Separate dev schemas for each source, created by the workflow |
| Stage agents | Stage runner job calling the `databricks-claude-sonnet-5-5` Foundation Model API endpoint |
| Pipeline execution | Lakeflow Jobs and Lakeflow Spark Declarative Pipelines |
| Validation queries and metric views | A SQL warehouse |
| Genie agents and benchmarks | The Genie API |

---

## 13. POC Success Criteria

- **Generality:** both test datasets complete all eight stages with no code changes.
- **Accuracy:** metric views reconcile with independent queries; the benchmark pass rate is at least 90% for each Genie agent.
- **Efficiency:** reviewer hours per source; median review time per card; share of proposals accepted unedited, per stage.
- **Clarity:** at least 90% of cards rated "clear".
- **Glossary:** a generated glossary reaches approval with a measurable edit rate (tracked, no fixed target).
- **Reuse:** a measurable drop in edits or rejections on the second dataset from decision library reuse.
- **Traceability:** every deployed artifact links to an approval and an agent trace.

---

## 14. POC Plan

**Test datasets:** NetSuite and Keka — used only as test inputs; nothing specific to either source goes into code, prompts or templates.

### Phase 1 — Skeleton
- App UI, stage controller, control tables, artifact writer (Volume + Git), packet format and validator, card renderer with both views, gate summary pages.
- Stages 1–3 on the first test dataset.

### Phase 2 — Semantic Layer
- Stages 4–8 on the first test dataset, ending with a working Genie agent at a 90% or higher benchmark pass rate.
- Add the chat panel on each card and the clarity feedback.

### Phase 3 — Generalization Test
- Run the second test dataset end to end, including the overlap assessment at the assignment gate.
- Capture effort, clarity, reuse and quality, and decide whether to scale.

---

## 15. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Agent infers incorrect business meaning | Mandatory review of high-risk items; checks against independent SQL; glossary review |
| Generated glossary is shallow or wrong | Dedicated review gate; definitions drawn from real data patterns; edits saved to the library |
| Benchmarks reflect the agent's own assumptions | Reviewer sign-off on expected answers; include questions written by people |
| Combined Genie agents get noisy or conflicting | Overlap rationale shown at the gate; checks for conflicting measure names; benchmarks across sources |
| Explanations too technical or too long | Business view by default, packet validation, glossary terms, clarity feedback |
| Review fatigue | Risk tiers, bulk approval, previously-approved labels, defer option |
| Generality not achieved | Phase 3 tests this explicitly; anything domain-specific must go into config, the glossary or the library, not code |
| Agent cost or latency | Cache profiles; regenerate only the stage being rerun and those after it |

---

## 16. Decision Log

| # | Decision |
|---|---|
| 1 | Test datasets: NetSuite and Keka (test inputs only; the design stays generic) |
| 2 | Benchmark pass-rate threshold: 90%. Card-clarity target: 90% |
| 3 | Artifacts stored in both a Unity Catalog Volume and Git |
| 4 | Run state and control data stored in Delta tables |
| 5 | When sources overlap, the default proposal is a combined Genie agent; the reviewer decides |
| 6 | Review audience is both business and technical; every card has both views |
| 7 | Glossary is optional at registration; if not provided, the workflow generates one and sends it for review |
| 8 | LLM for all stage agents: `databricks-claude-sonnet-5-5` (Foundation Model APIs), with a small custom tool-calling loop and MLflow tracing |
| 9 | App framework: Streamlit (Python only, same language as the agents, native chat and form components, fastest to build for a POC) |
| 10 | Agents run in a generic stage runner job started by the app, not inside app requests |
| 11 | Deterministic Python computes facts and checks; the LLM interprets, proposes options and writes packets |
| 12 | Git commits come from the stage runner via the GitHub REST API with a repo-scoped token in a secret scope, so runs need no interactive user |
| 13 | The app and job service principal creates and owns the `semantic_builder_poc` catalog; all writes are create-only (no overwrite or drop) |
| 14 | Generated silver and gold code is SQL in one Lakeflow Spark Declarative Pipeline per source, writing to dev schemas |

---

## 17. Remaining Open Questions

1. Confirm that the 90% card-clarity target is correct.
2. What maximum number of redraft attempts should a rejected card get before manual handling?
3. What overlap score should trigger the "combine" default at the assignment gate?
