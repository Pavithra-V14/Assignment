from __future__ import annotations

from project_health_agent.ingestion.data_loader import load_project_plan
from project_health_agent.scoring.metrics import (
    apply_overrides,
    compute_composite,
    compute_signals,
)


def _score(path: str) -> dict:
    project = load_project_plan(path)
    signals = compute_signals(project)
    composite = compute_composite(signals)
    overrides = apply_overrides(composite, signals, source_rag=project["summary"].get("RAG"))
    return {
        "composite_score": composite["composite_score"],
        "final_band": overrides["final_band"],
    }


def test_composite_score_is_bounded(project_a_path: str) -> None:
    result = _score(project_a_path)
    assert 0 <= result["composite_score"] <= 100
    assert result["final_band"] in {"Green", "Amber", "Red"}


def test_composite_score_is_deterministic(project_a_path: str) -> None:
    # Same input -> same output, every time. This is the entire point of
    # keeping scoring LLM-free: a PM must be able to reproduce the number.
    first = _score(project_a_path)
    second = _score(project_a_path)
    assert first["composite_score"] == second["composite_score"]
    assert first["final_band"] == second["final_band"]


def test_project_b_scores_red_from_known_slip(project_b_path: str) -> None:
    # Documented in README: Project B's own file says "Green" while the
    # engine independently computes "Red" from a large slip on a zero-float
    # critical task plus unresolved blockers. Locking this in as a
    # regression test — if scoring logic changes and this stops being Red,
    # that's a signal to re-check the change, not silently accept it.
    result = _score(project_b_path)
    assert result["final_band"] == "Red"
