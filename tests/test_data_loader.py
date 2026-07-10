from __future__ import annotations

import pandas as pd

from project_health_agent.ingestion.data_loader import load_project_plan


def test_load_project_plan_returns_expected_shape(project_a_path: str) -> None:
    result = load_project_plan(project_a_path)

    assert result["project_name"]
    assert isinstance(result["tasks"], pd.DataFrame)
    assert len(result["tasks"]) > 0
    assert 0.0 <= result["data_completeness"] <= 1.0
    assert result["source_path"] == project_a_path


def test_load_project_plan_cleans_unparseable_cells(project_a_path: str) -> None:
    result = load_project_plan(project_a_path)
    tasks = result["tasks"]
    # #UNPARSEABLE and blank strings must be normalized to None/NaN, never
    # survive as literal strings, since downstream scoring treats them as
    # missing data.
    for col in tasks.columns:
        assert not (tasks[col] == "#UNPARSEABLE").any()


def test_load_project_plan_parses_duration_days(project_a_path: str) -> None:
    result = load_project_plan(project_a_path)
    tasks = result["tasks"]
    if "duration_days" in tasks.columns:
        non_null = tasks["duration_days"].dropna()
        assert (non_null.apply(lambda v: isinstance(v, float))).all()
