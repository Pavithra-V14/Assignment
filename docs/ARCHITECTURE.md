# Architecture

## Package layout

```
src/project_health_agent/
├── core/            Settings (pydantic-settings), logging, typed exceptions.
│                     Every other package imports FROM here; core imports
│                     nothing from the rest of the package (no cycles).
├── ingestion/        Turns "a data source" into "local .xlsx paths",
│                     then "a local .xlsx path" into a cleaned DataFrame.
│                       source.py        - local vs. Drive switch
│                       drive_client.py  - Google Drive folder sync
│                       data_loader.py   - workbook parsing/cleaning
├── scoring/          Deterministic RAG scoring engine. No LLM calls, no I/O
│                     beyond what's passed in — pure functions over the
│                     cleaned DataFrame, so they're cheap to unit test.
│                       metrics.py, semantic_signals.py
├── reasoning/         The one LLM call per weekly report: cross-checks the
│                     deterministic band, explains drivers, recommends
│                     actions. Provider-agnostic (Groq/Gemini/fallback).
│                       llm_client.py, graph.py (LangGraph state machine)
├── reporting/         Deterministic rendering of a finished report into a
│                     .docx (weekly) or contributes to the .pptx (monthly).
│                       docx_builder.py, deck_builder.py
├── aggregation/       Phase 3: reads ALL weekly JSON outputs, computes
│                     trend/theme deltas deterministically, then one LLM
│                     call turns that evidence into slide-ready JSON.
│                       aggregator.py, synthesis_agent.py
└── cli/              Typer entrypoints wiring the above into runnable
                      commands (console scripts, see pyproject.toml).
                        weekly.py, monthly.py
```

Dependency direction is one-way: `cli -> {ingestion, reasoning, reporting,
aggregation} -> scoring -> core`. Nothing in `scoring/` or `core/` imports
from a higher layer — that's what keeps the scoring engine testable in
isolation and safe to reuse if a second interface (e.g. an internal API)
is added later.

## Where the two LLM calls sit, and why only two

```
Phase 2 (weekly, per project)                 Phase 3 (monthly, portfolio-wide)
------------------------------                ----------------------------------
ingestion.source.get_project_plan_paths()
      |
      v
ingestion.data_loader   -- deterministic --
      |
      v
scoring.metrics         -- deterministic, auditable weighted score + overrides
      |
      v
reasoning.graph (LLM)   -- ONE call: cross-checks the band, explains
      |                    drivers in plain English, recommends actions
      v
reporting + JSON  -----------------------> <WEEKLY_OUTPUT_DIR>/<project>/<date>.json
                                                       |
                                                       v
                                              aggregation.aggregator -- deterministic
                                              (reads ALL weekly JSON, computes
                                               trend + recurring themes)
                                                       |
                                                       v
                                              aggregation.synthesis_agent (LLM)
                                              -- ONE call, slide-ready JSON
                                                       |
                                                       v
                                              reporting.deck_builder -- deterministic
                                                       |
                                                       v
                                    <MONTHLY_OUTPUT_DIR>/Executive_Portfolio_Review.pptx
```

Everything left of an LLM call is deterministic Python. The LLM is never
asked to invent numbers, parse a spreadsheet, or produce a binary file
format — see the README's "Design decisions" section for the reasoning
behind that split.

## Configuration model

A single `Settings` object (`core/config.py`, pydantic-settings) is the only
thing in the codebase that reads environment variables. It's populated from,
in order of precedence: real environment variables > `.env` file. This means:

- Local dev: copy `.env.example` to `.env`.
- CI / containers: inject env vars directly (repo secrets/variables); no
  `.env` file needed or committed.
- A missing/misspelled required setting (e.g. `DATA_SOURCE=drive` without
  `DRIVE_FOLDER_URL`) fails immediately at startup with a clear message,
  rather than surfacing as a confusing error mid-pipeline.

Business-tunable constants (scoring weights, band thresholds, override
rules) are intentionally *not* environment variables — they're versioned
Python constants reviewed in pull requests, since they define what "Red"
means and that shouldn't be changeable by an untracked env var. See the
comment at the top of `core/config.py`.

## Observability

- Structured logging (`core/logging_config.py`) to stdout, `LOG_FORMAT=json`
  for log-aggregator-friendly output in scheduled/containerized runs.
- Every weekly/monthly output JSON records exactly which mode produced it
  (`generated_by`: `groq:<model>` / `gemini:<model>` / `fallback_template`),
  so no output is ever ambiguous about whether a real model was involved —
  useful both for debugging and for auditing report provenance.
- Typed exceptions (`core/exceptions.py`) let the CLI layer distinguish
  "data source unreachable, retry later" from "spreadsheet malformed, needs
  a human" and set process exit codes accordingly, instead of a catch-all.

## What would change at larger scale

- **Orchestration**: GitHub Actions cron (`.github/workflows/`) is enough
  for two scheduled jobs. At real portfolio scale (dozens of teams, cross-
  pipeline dependencies, retries with backoff/alerting), this would move to
  Airflow/Prefect/Dagster — the CLI entrypoints are already orchestrator-
  agnostic (plain Python functions with clean exit codes), so that move
  doesn't require rewriting agent logic, only the trigger.
- **State**: outputs are currently JSON files on disk (`var/outputs/`). A
  database (Postgres) would replace the filesystem as the source of truth
  once report history needs to be queried rather than just aggregated by
  `aggregator.py`'s file-glob.
- **Secrets**: `.env` / repo secrets are adequate for this size; a managed
  secrets store (AWS Secrets Manager, GCP Secret Manager, Vault) would
  replace `GOOGLE_SERVICE_ACCOUNT_JSON_BASE64` and the LLM API keys once
  there's more than one deployment environment to keep in sync.
