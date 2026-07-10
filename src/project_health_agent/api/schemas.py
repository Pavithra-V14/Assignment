"""
Pydantic response/request models for the HTTP API.

Kept deliberately thin: the pipeline's own dicts (weekly report,
portfolio package, slide plan) are already the contract every other
part of the system (docx/pptx builders, tests) relies on, so the API
mostly passes them through as `dict` rather than re-declaring a
parallel schema that could drift out of sync. Models here describe
the API's *own* shapes: list/summary views, job status, errors.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    environment: str
    data_source: str
    llm_provider: str


class WeeklyReportSummary(BaseModel):
    project_slug: str
    project_name: str
    week_ending: str
    final_rag_status: str | None = None
    composite_score: float | None = None
    generated_at_utc: str | None = None
    generated_by: str | None = None


class RunWeeklyRequest(BaseModel):
    path: str | None = Field(
        default=None,
        description="Explicit path to a single .xlsx file on the server. "
        "If omitted, every plan from the configured DATA_SOURCE is processed.",
    )


class RunWeeklyResponse(BaseModel):
    processed: int
    failed: int
    reports: list[dict[str, Any]]
    errors: list[dict[str, str]] = Field(default_factory=list)


class RunMonthlyRequest(BaseModel):
    month: str | None = Field(
        default=None,
        description="Calendar month to report on, as 'YYYY-MM'. Defaults to the current month.",
    )


class RunMonthlyResponse(BaseModel):
    month: str
    portfolio_package: dict[str, Any]
    slide_plan: dict[str, Any]
    deck_path: str


class ErrorResponse(BaseModel):
    detail: str
    error_type: str
