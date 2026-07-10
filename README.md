# Project Health Reporting Agent

An AI agent that reads project plan exports (from a local folder **or** a
live company Google Drive folder), independently determines a RAG
(Red/Amber/Green) status with plain-English reasoning, and automatically
synthesizes a monthly executive presentation across the whole portfolio.

For the full pipeline diagram and package layout, see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). For connecting a Google
Drive folder as the data source, see
[`docs/GOOGLE_DRIVE_SETUP.md`](docs/GOOGLE_DRIVE_SETUP.md).

---

## 1. Quick start (local files, no cloud setup)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # optional — see "LLM provider" below

project-health-weekly        # Phase 2: scores every plan in sample_data/
project-health-monthly       # Phase 3: aggregates + generates the executive deck
```

Outputs land in `var/outputs/`:
- `weekly/<project_slug>/<date>.json` and `.docx` — one per project, per run
- `monthly/portfolio_package.json` — the aggregated trend/risk data
- `monthly/slide_plan.json` — the LLM-authored slide content (auditable, before rendering)
- `monthly/Executive_Portfolio_Review.pptx` — the final deck

To score a single specific file: `project-health-weekly --path path/to/plan.xlsx`

## 2. Pointing it at a live company Google Drive folder instead

This is the production path: a PM keeps the project plan updated in a
shared Drive folder, and the agent's scheduled run just picks up whatever
is current there — nobody manually uploads or attaches a file.

```bash
pip install -e ".[drive]"
```

```dotenv
# .env
DATA_SOURCE=drive
DRIVE_FOLDER_URL=https://drive.google.com/drive/folders/<your-folder-id>
GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account.json
```

Full walkthrough (creating the service account, sharing the folder, wiring
it into CI) is in [`docs/GOOGLE_DRIVE_SETUP.md`](docs/GOOGLE_DRIVE_SETUP.md).
Nothing else changes: `project-health-weekly` behaves identically whether
`DATA_SOURCE` is `local` or `drive` — see `ingestion/source.py`.

## 3. HTTP API (FastAPI)

Everything above is also available over HTTP, for a UI or any other
integration to call. The API is a thin layer — every route calls straight
into the same functions the CLIs use, so scheduled runs and API-triggered
runs can never silently diverge in behavior (see
`src/project_health_agent/api/main.py`).

```bash
pip install -e ".[dev]"
make api                      # uvicorn on http://localhost:8000
# or: uvicorn project_health_agent.api.main:app --reload --port 8000
```

Interactive docs (Swagger UI) at `http://localhost:8000/docs`. Endpoints:

| Method | Path                                          | Does |
|--------|-----------------------------------------------|------|
| GET    | `/health`                                     | environment, data source, LLM provider |
| GET    | `/projects`                                   | distinct projects with a weekly report |
| POST   | `/weekly/run`                                 | score every plan from `DATA_SOURCE`, or one `{"path": "..."}` |
| POST   | `/weekly/upload`                              | upload a single `.xlsx`, score it directly |
| GET    | `/weekly/reports`                             | every weekly report summary on disk |
| GET    | `/weekly/reports/{project_slug}`              | full history for one project |
| GET    | `/weekly/reports/{project_slug}/{week}`       | one report's full JSON |
| GET    | `/weekly/reports/{project_slug}/{week}/docx`  | download that report's `.docx` |
| POST   | `/monthly/run`                                | aggregate + synthesize + render the deck |
| GET    | `/monthly/package`                            | latest portfolio package JSON |
| GET    | `/monthly/slide_plan`                         | latest LLM-authored slide plan |
| GET    | `/monthly/deck`                                | download `Executive_Portfolio_Review.pptx` |

Runs are synchronous — one workbook is seconds of work, not minutes, so a
background job queue would be premature here (see `docs/ARCHITECTURE.md`
for the orchestrator trade-off at real portfolio scale).

## 4. Streamlit frontend

A small multipage Streamlit app (`frontend/`) that is a pure client of the
API above — it does no scoring, parsing, or LLM calls itself.

```bash
pip install -e ".[dev,frontend]"
make api                      # in one terminal
make frontend                 # in another: streamlit run frontend/app.py
```

Opens at `http://localhost:8501`. Pages:
- **Home** — portfolio dashboard: RAG mix, composite scores by project, recurring themes/risks
- **Weekly Reports** — browse any project/week's full report: signal breakdown, plain-English reasoning, recommended actions, `.docx` download
- **Run Agent** — run the configured data source, or upload one `.xlsx` directly
- **Monthly Synthesis** — trigger Phase 3, preview the slide plan, download the `.pptx` deck

Point it at a non-default API by setting `PHA_API_BASE_URL` (see `.env.example`).

## 5. LLM provider (Groq or Gemini)

```dotenv
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
```
or
```dotenv
LLM_PROVIDER=gemini
GEMINI_API_KEY=AI...
```

**If left blank, the pipeline still runs completely** — every LLM call has a
deterministic, template-based fallback (see `reasoning/llm_client.py`).
Every output JSON records exactly which mode produced it via a
`"generated_by"` field (`groq:llama-3.3-70b-versatile`, `gemini:gemini-2.0-flash`,
or `fallback_template`), so output is never ambiguous about whether a real
model was involved. This is deliberate, not a shortcut: the agent is fully
testable/CI-able without exposing an API key, and degrades gracefully in
production if the LLM API has an outage mid-week rather than blocking the
whole report.

## 6. Running on a schedule

`.github/workflows/weekly_report.yml` and `monthly_synthesis.yml` run the
two CLIs on cron schedules via GitHub Actions. Add `GROQ_API_KEY` /
`GEMINI_API_KEY` / `GOOGLE_SERVICE_ACCOUNT_JSON_BASE64` as repo secrets and
`LLM_PROVIDER` / `DATA_SOURCE` / `DRIVE_FOLDER_URL` as repo variables.

At larger scale this would move to an orchestrator like Airflow, Prefect, or
Dagster for DAG-level retries, alerting, and cross-pipeline dependencies —
see `docs/ARCHITECTURE.md` for that trade-off discussion.

## 7. Development

```bash
pip install -e ".[dev]"
pytest                 # unit tests (scoring engine, data cleaning, Drive sync — all mocked, no network)
ruff check src tests   # lint
mypy src                # type check
```

Everything here runs directly with Python — no Docker or container runtime
required.

---

## 8. Design decisions (the "why", not just the "what")

**Why deterministic scoring + one LLM call, not an LLM doing everything.**
Composite RAG scores need to be reproducible and auditable — a PM should be
able to see exactly why a project is Red and reconstruct that from raw data.
That logic lives entirely in `scoring/metrics.py`, with zero LLM
involvement. The LLM is used only for the parts language models are
actually good at: explaining evidence in plain English, and independently
sanity-checking the computed band. If the model's opinion disagrees with
the deterministic band, that disagreement is surfaced in the report
(`model_vs_deterministic_disagreement`) — never silently overwritten in
either direction.

**Why classification and reasoning are one LLM call, not two.** A separate
"classifier" call and "explainer" call double latency/cost and create a
subtle failure mode: the explainer ends up rationalizing whatever status
it's handed, rather than reasoning independently. One prompt that reasons
over the evidence and states its own opinion (see `reasoning/graph.py`'s
system prompt) is both cheaper and more honest — and because the
deterministic score is authoritative regardless, the model's opinion is a
cross-check, not a decision.

**Why the existing "Schedule Health"/"RAG" columns in source files are not
trusted as ground truth.** The agent computes its own status independently
and flags disagreement (`source_vs_computed_disagreement` in the weekly
JSON) rather than copying the existing label — the point of the agent is to
catch cases where the source status is stale or optimistic, not reproduce
it.

**Why Phase 3 never re-reads the raw Excel files.** The monthly aggregator
only reads the JSON that Phase 2 already produced. This is what makes
"weekly reporting" and "monthly synthesis" composable: Phase 3 is
aggregating structured history, not re-parsing spreadsheets, so it works
identically whether there are 2 projects or 200, and regardless of whether
the underlying source is a local folder or a live Drive folder.

**Why slide generation is a two-step LLM-then-code process, not "ask the
model for a pptx".** The LLM returns structured JSON matching a fixed slide
schema (`aggregation/synthesis_agent.py`); `reporting/deck_builder.py`
deterministically renders that JSON with `python-pptx`, including a native
RAG-distribution pie chart. This reliably guarantees consistent slide count
and formatting — asking a model to produce binary Office file content
directly is unreliable and unnecessary when the actual creative work
(deciding what the story is) is separable from rendering.

**Why trend detection and theme-clustering are deterministic, and the LLM
only writes the narrative.** `aggregation/aggregator.py` computes RAG
movement, score deltas, and recurring themes across projects in plain
Python (theme matching is semantic/exemplar-based via BM25 — see
`scoring/semantic_signals.py`, not a fixed keyword list) before the LLM
ever sees the data.

**Why the data source is a swappable seam (`ingestion/source.py`), not
hardcoded to a folder.** The original version of this project only ever
read `sample_data/*.xlsx`. In a real company, files don't get manually
copied around — they live in a shared Drive folder that the PM keeps
current. `ingestion/drive_client.py` makes `DATA_SOURCE=drive` a one-line
config change: it lists and syncs whatever is currently in a Drive folder,
incrementally (unchanged files aren't re-downloaded) and with deletions
mirrored, then hands the same list-of-local-paths contract to
`data_loader.py` that the local mode always did. Nothing downstream knows
or cares which source produced the files.

### Known limitation, stated rather than hidden

Trend detection needs at least two weekly runs per project to show real
week-over-week movement — the schema and logic
(`aggregator.py::_band_trend`) are already built for it, it just needs
history to populate. What's demonstrated from a single run is cross-project
comparison and recurring-theme detection, which does work from one
snapshot.

There is also no budget/cost column in either sample source file — see
`docs/Phase1_RAG_Methodology.docx` for how that gap is handled in the
scoring methodology (excluded rather than estimated, noted per report).

---

## 9. Project layout

```
project-health-agent/
├── pyproject.toml              # packaging, dependencies, tool config (ruff/mypy/pytest)
├── Makefile                    # make install / test / lint / run-weekly / run-monthly / api / frontend
├── .env.example
├── .github/workflows/          # ci.yml (lint+test on PR), weekly_report.yml, monthly_synthesis.yml
├── docs/
│   ├── ARCHITECTURE.md
│   ├── GOOGLE_DRIVE_SETUP.md
│   ├── Phase1_RAG_Methodology.docx
│   └── example_outputs/        # reference outputs from the original take-home submission
├── sample_data/                 # local-mode sample project plans (also used by tests)
├── src/project_health_agent/    # see docs/ARCHITECTURE.md for the full breakdown
│   ├── core/                    # settings, logging, exceptions
│   ├── ingestion/                # data source (local/Drive) + workbook cleaning
│   ├── scoring/                  # deterministic RAG scoring engine
│   ├── reasoning/                 # LLM reasoning layer (Phase 2)
│   ├── reporting/                 # .docx / .pptx rendering
│   ├── aggregation/                # Phase 3: trend detection + slide synthesis
│   ├── cli/                        # Typer entrypoints (console scripts)
│   └── api/                        # FastAPI app — thin HTTP layer over the above
├── frontend/                     # Streamlit app, a pure client of api/ over HTTP
│   ├── app.py                     # Home: portfolio dashboard
│   ├── api_client.py               # shared requests wrapper
│   └── pages/                      # Weekly Reports / Run Agent / Monthly Synthesis
├── tests/                        # pytest, no network required (includes test_api.py)
└── var/                          # runtime output + cache (gitignored)
    ├── outputs/{weekly,monthly}/
    └── cache/drive/
```

