"""
Smoke tests for the FastAPI layer.

These exercise the full weekly -> monthly pipeline through HTTP, same as
a real client would, but redirect WEEKLY_OUTPUT_DIR/MONTHLY_OUTPUT_DIR to
a tmp dir so runs never write into the repo's real var/outputs/. No LLM
provider is configured in test settings, so every run exercises the
deterministic fallback path (see reasoning/llm_client.py) — no network
required, consistent with the rest of this test suite.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch, project_a_path, project_b_path):
    weekly_dir = tmp_path / "weekly"
    monthly_dir = tmp_path / "monthly"

    # Patch settings on every module that already imported `settings` by
    # reference (config.py's module-level singleton), matching how the
    # CLIs/aggregator/API all consume it.
    from project_health_agent.core.config import settings

    monkeypatch.setattr(settings, "WEEKLY_OUTPUT_DIR", str(weekly_dir))
    monkeypatch.setattr(settings, "MONTHLY_OUTPUT_DIR", str(monthly_dir))
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(Path(project_a_path).parent))

    from project_health_agent.api.main import app

    return TestClient(app)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["data_source"] == "local"


def test_no_reports_yet(client):
    resp = client.get("/weekly/reports")
    assert resp.status_code == 200
    assert resp.json() == []

    resp = client.get("/monthly/package")
    assert resp.status_code == 404


def test_weekly_run_then_list_then_monthly(client):
    run_resp = client.post("/weekly/run", json={})
    assert run_resp.status_code == 200
    run_body = run_resp.json()
    assert run_body["processed"] == 2
    assert run_body["failed"] == 0

    list_resp = client.get("/weekly/reports")
    assert list_resp.status_code == 200
    summaries = list_resp.json()
    assert len(summaries) == 2
    assert {s["final_rag_status"] for s in summaries} <= {"Green", "Amber", "Red"}

    slug = summaries[0]["project_slug"]
    week = summaries[0]["week_ending"]
    detail_resp = client.get(f"/weekly/reports/{slug}/{week}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["project_name"]

    docx_resp = client.get(f"/weekly/reports/{slug}/{week}/docx")
    assert docx_resp.status_code == 200
    assert docx_resp.headers["content-type"].startswith("application/vnd.openxmlformats")

    monthly_resp = client.post("/monthly/run")
    assert monthly_resp.status_code == 200
    monthly_body = monthly_resp.json()
    assert monthly_body["month"]
    assert monthly_body["portfolio_package"]["generated_from_projects"] == 2
    assert monthly_body["portfolio_package"]["month"] == monthly_body["month"]
    assert len(monthly_body["slide_plan"]["slides"]) > 0

    deck_resp = client.get("/monthly/deck")
    assert deck_resp.status_code == 200

    # The report just generated should show up as the current month, both
    # as a candidate (weekly data exists) and as already-generated.
    months_resp = client.get("/monthly/months")
    assert months_resp.status_code == 200
    months_body = months_resp.json()
    assert monthly_body["month"] in months_body["with_weekly_data"]
    assert monthly_body["month"] in months_body["generated"]

    # Explicitly asking for a month with no weekly data at all fails with
    # a clear, catchable error rather than aggregating everything.
    other_month_resp = client.post("/monthly/run", json={"month": "2019-01"})
    assert other_month_resp.status_code == 422

    # Package/slide_plan/deck for an unrequested-but-invalid month 404
    # rather than silently falling back to some other month's data.
    stale_pkg_resp = client.get("/monthly/package", params={"month": "2019-01"})
    assert stale_pkg_resp.status_code == 404


def test_weekly_run_single_explicit_path(client, project_a_path):
    resp = client.post("/weekly/run", json={"path": project_a_path})
    assert resp.status_code == 200
    body = resp.json()
    assert body["processed"] == 1


def test_upload_rejects_non_xlsx(client):
    resp = client.post(
        "/weekly/upload",
        files={"file": ("not_a_plan.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400


def test_unknown_project_404(client):
    resp = client.get("/weekly/reports/does_not_exist")
    assert resp.status_code == 404
