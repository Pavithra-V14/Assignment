"""
FastAPI layer over the existing weekly/monthly pipeline.

Design intent, mirroring the rest of the codebase's "thin orchestration,
real logic elsewhere" philosophy:

- This module contains NO scoring, reasoning, or rendering logic of its
  own. Every route calls straight into the same functions the CLIs use
  (`cli.weekly.process_one`, `cli.monthly.run_monthly`,
  `ingestion.source.get_project_plan_paths`,
  `aggregation.aggregator.load_all_project_histories`) so the API and the
  scheduled CLI runs can never silently diverge in behavior.
- Reads (`GET /weekly/reports`, `GET /monthly/package`, ...) are pure
  filesystem reads of what Phase 2/3 already wrote to
  `WEEKLY_OUTPUT_DIR` / `MONTHLY_OUTPUT_DIR` — the API doesn't maintain
  its own database or duplicate state.
- Writes (`POST /weekly/run`, `POST /weekly/upload`, `POST /monthly/run`)
  are synchronous. Each weekly run is one workbook -> one deterministic
  score -> one LLM call (or fallback) -> one docx, which is seconds, not
  minutes, so a background job queue would be premature complexity here;
  see docs/ARCHITECTURE.md for the orchestrator trade-off discussion that
  would apply at real portfolio scale.

Run with:
    uvicorn project_health_agent.api.main:app --reload --port 8000
or:
    make api
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from project_health_agent.aggregation.aggregator import list_available_months, load_all_project_histories
from project_health_agent.cli.monthly import run_monthly
from project_health_agent.cli.weekly import process_one
from project_health_agent.core.config import settings
from project_health_agent.core.exceptions import ProjectHealthAgentError
from project_health_agent.core.logging_config import configure_logging, get_logger
from project_health_agent.ingestion.source import get_project_plan_paths

from .schemas import (
    HealthResponse,
    RunMonthlyRequest,
    RunMonthlyResponse,
    RunWeeklyRequest,
    RunWeeklyResponse,
    WeeklyReportSummary,
)

configure_logging()
logger = get_logger("api.main")

app = FastAPI(
    title="Project Health Reporting Agent API",
    description=(
        "HTTP API over the weekly RAG-scoring agent and the monthly "
        "portfolio synthesis agent. See /docs for interactive Swagger UI."
    ),
    version="1.0.0",
)

# Local-first tool: the Streamlit frontend (and any other local client)
# talks to this API from a different port, so CORS needs to be open for
# local dev. Tighten `allow_origins` before deploying behind a real
# frontend origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _error_response(exc: ProjectHealthAgentError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"detail": str(exc), "error_type": type(exc).__name__},
    )


# --------------------------------------------------------------------------
# Health / config
# --------------------------------------------------------------------------
@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    return HealthResponse(
        environment=settings.ENVIRONMENT,
        data_source=settings.DATA_SOURCE,
        llm_provider=settings.LLM_PROVIDER,
    )


# --------------------------------------------------------------------------
# Weekly (Phase 2)
# --------------------------------------------------------------------------
@app.get(
    "/weekly/reports",
    response_model=list[WeeklyReportSummary],
    tags=["weekly"],
    summary="List every weekly report on disk, across all projects.",
)
def list_weekly_reports() -> list[WeeklyReportSummary]:
    histories = load_all_project_histories()
    summaries: list[WeeklyReportSummary] = []
    for slug, history in histories.items():
        for report in history:
            summaries.append(
                WeeklyReportSummary(
                    project_slug=slug,
                    project_name=report.get("project_name", slug),
                    week_ending=report.get("generated_at_utc", "")[:10],
                    final_rag_status=report.get("final_rag_status"),
                    composite_score=report.get("composite_score"),
                    generated_at_utc=report.get("generated_at_utc"),
                    generated_by=report.get("generated_by"),
                )
            )
    return summaries


@app.get(
    "/weekly/reports/{project_slug}",
    tags=["weekly"],
    summary="Full weekly history (all runs) for a single project.",
)
def get_project_history(project_slug: str) -> list[dict[str, Any]]:
    histories = load_all_project_histories()
    if project_slug not in histories:
        raise HTTPException(status_code=404, detail=f"No reports found for project '{project_slug}'.")
    return histories[project_slug]


@app.get(
    "/weekly/reports/{project_slug}/{week_ending}",
    tags=["weekly"],
    summary="A single weekly report JSON.",
)
def get_weekly_report(project_slug: str, week_ending: str) -> dict[str, Any]:
    json_path = Path(settings.WEEKLY_OUTPUT_DIR) / project_slug / f"{week_ending}.json"
    if not json_path.exists():
        raise HTTPException(status_code=404, detail=f"No report at {json_path}")
    with open(json_path) as f:
        return json.load(f)


@app.get(
    "/weekly/reports/{project_slug}/{week_ending}/docx",
    tags=["weekly"],
    summary="Download the rendered .docx for a single weekly report.",
)
def download_weekly_docx(project_slug: str, week_ending: str) -> FileResponse:
    docx_path = Path(settings.WEEKLY_OUTPUT_DIR) / project_slug / f"{week_ending}.docx"
    if not docx_path.exists():
        raise HTTPException(status_code=404, detail=f"No docx at {docx_path}")
    return FileResponse(
        docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=docx_path.name,
    )


@app.post(
    "/weekly/run",
    response_model=RunWeeklyResponse,
    tags=["weekly"],
    summary="Run the weekly agent — every plan from DATA_SOURCE, or one explicit path.",
)
def run_weekly(request: RunWeeklyRequest | None = Body(default=None)) -> RunWeeklyResponse:
    explicit_path = request.path if request else None
    try:
        paths = get_project_plan_paths(explicit_path=explicit_path)
    except ProjectHealthAgentError as exc:
        raise _error_response(exc) from exc

    reports: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for p in paths:
        try:
            reports.append(process_one(p))
        except ProjectHealthAgentError as exc:
            errors.append({"path": p, "error": str(exc), "error_type": type(exc).__name__})
            logger.error("Failed to process %s: %s", p, exc)

    return RunWeeklyResponse(processed=len(reports), failed=len(errors), reports=reports, errors=errors)


@app.post(
    "/weekly/upload",
    tags=["weekly"],
    summary="Upload a single .xlsx project plan and run the weekly agent on it.",
)
async def upload_and_run_weekly(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Only .xlsx files are accepted.")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / file.filename
        with open(tmp_path, "wb") as out:
            shutil.copyfileobj(file.file, out)
        try:
            return process_one(str(tmp_path))
        except ProjectHealthAgentError as exc:
            raise _error_response(exc) from exc


# --------------------------------------------------------------------------
# Monthly (Phase 3)
# --------------------------------------------------------------------------
# Each monthly run is scoped to one calendar month and lives under its own
# <MONTHLY_OUTPUT_DIR>/<YYYY-MM>/ subfolder (see cli/monthly.py), so running
# a later month never clobbers an earlier one and `GET /monthly/*` can be
# asked for any month that's already been generated, not just "whatever
# was generated most recently."


def _generated_months() -> list[str]:
    """Months that already have a rendered portfolio package on disk,
    newest first."""
    base = Path(settings.MONTHLY_OUTPUT_DIR)
    if not base.is_dir():
        return []
    months = [
        p.name for p in base.iterdir()
        if p.is_dir() and (p / "portfolio_package.json").exists()
    ]
    return sorted(months, reverse=True)


def _resolve_month(month: str | None) -> str:
    """An explicit month if given, else the most recently generated one."""
    if month:
        return month
    generated = _generated_months()
    if not generated:
        raise HTTPException(
            status_code=404,
            detail="No monthly report has been generated yet — run POST /monthly/run first.",
        )
    return generated[0]


@app.get(
    "/monthly/months",
    tags=["monthly"],
    summary="Calendar months available to report on / already reported on.",
)
def list_months() -> dict[str, list[str]]:
    return {
        "with_weekly_data": list_available_months(),
        "generated": _generated_months(),
    }


@app.get(
    "/monthly/package",
    tags=["monthly"],
    summary="Aggregated portfolio package for one month (trend/risk data across all projects).",
)
def get_portfolio_package(month: str | None = None) -> dict[str, Any]:
    resolved = _resolve_month(month)
    path = Path(settings.MONTHLY_OUTPUT_DIR) / resolved / "portfolio_package.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No portfolio package for {resolved} — run POST /monthly/run first.",
        )
    return json.loads(path.read_text())


@app.get(
    "/monthly/slide_plan",
    tags=["monthly"],
    summary="LLM-authored slide plan for one month (auditable, pre-rendering).",
)
def get_slide_plan(month: str | None = None) -> dict[str, Any]:
    resolved = _resolve_month(month)
    path = Path(settings.MONTHLY_OUTPUT_DIR) / resolved / "slide_plan.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No slide plan for {resolved} — run POST /monthly/run first.",
        )
    return json.loads(path.read_text())


@app.get(
    "/monthly/deck",
    tags=["monthly"],
    summary="Download the executive portfolio review .pptx deck for one month.",
)
def download_deck(month: str | None = None) -> FileResponse:
    resolved = _resolve_month(month)
    path = Path(settings.MONTHLY_OUTPUT_DIR) / resolved / "Executive_Portfolio_Review.pptx"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No deck for {resolved} — run POST /monthly/run first.",
        )
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"Executive_Portfolio_Review_{resolved}.pptx",
    )


@app.post(
    "/monthly/run",
    response_model=RunMonthlyResponse,
    tags=["monthly"],
    summary="Aggregate one month's weekly outputs, synthesize the narrative, and render the deck.",
)
def run_monthly_synthesis(request: RunMonthlyRequest | None = Body(default=None)) -> RunMonthlyResponse:
    month = request.month if request else None
    try:
        result = run_monthly(month=month)
    except ProjectHealthAgentError as exc:
        raise _error_response(exc) from exc
    return RunMonthlyResponse(**result)


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------
@app.get(
    "/projects",
    tags=["projects"],
    summary="Distinct projects that have at least one weekly report.",
)
def list_projects() -> list[dict[str, str]]:
    histories = load_all_project_histories()
    out = []
    for slug, history in histories.items():
        name = history[-1].get("project_name", slug) if history else slug
        out.append({"project_slug": slug, "project_name": name, "runs": str(len(history))})
    return out
