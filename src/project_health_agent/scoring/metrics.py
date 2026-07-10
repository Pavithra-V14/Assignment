"""
Deterministic metrics engine — implements the Phase 1 RAG methodology.

Every signal is scored 0 (healthy) / 1 (at risk) / 2 (critical), exactly as
defined in Phase1_RAG_Methodology.docx. This module does NOT call an LLM —
composite scoring should be reproducible and auditable, not subject to model
variance. The LLM's job (see llm_client.py / graph.py) is to explain these
numbers in plain English and adjudicate genuinely ambiguous cases, not to
invent them.
"""
from datetime import date, datetime
from typing import Any, cast

import pandas as pd

from project_health_agent.core.config import BAND_THRESHOLDS, OVERRIDE_RULES, SIGNAL_WEIGHTS
from project_health_agent.scoring.semantic_signals import is_blocker_text, is_negative_sentiment_text


def _to_date(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time())
    return None


def _clean(value):
    """Pandas gives back NaN (a float), not None, for a missing cell —
    python-docx chokes trying to write a float as run text. Normalize both
    to None so every consumer of _task_label/_task_detail can rely on
    'missing' always being None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


def _task_label(row: pd.Series) -> str:
    """'<task name> (Owner: <owner>)' — the compact, human-readable
    identifier used whenever a signal names a specific task, so a reader
    always knows exactly which task/row a driver is about instead of a
    bare day-count or task count."""
    name = _clean(row.get("task_name")) or "Unnamed task"
    owner = _clean(row.get("owner")) or _clean(row.get("assigned_to"))
    return f"{name} (Owner: {owner})" if owner else str(name)


def _task_detail(row: pd.Series, extra: dict | None = None) -> dict:
    """Structured evidence for one task — used in signal 'examples' lists so
    the docx/pptx renderers and the LLM prompt can cite task name, owner,
    and phase without re-deriving them from a DataFrame."""
    detail = {
        "task_name": _clean(row.get("task_name")) or "Unnamed task",
        "owner": _clean(row.get("owner")) or _clean(row.get("assigned_to")),
        "phase_milestone": _clean(row.get("phase_milestone")),
        "status": _clean(row.get("status")),
    }
    if extra:
        detail.update(extra)
    return detail


def _critical_mask(tasks: pd.DataFrame) -> pd.Series:
    """Single authoritative definition of 'on the critical path' — shared by
    _score_schedule_slippage (zero-float override) and _score_critical_path
    (critical_path_exposure signal), so the two signals can never disagree
    about which tasks are critical. Previously each signal used a different
    column (total_float vs. the source file's 'Critical ?' flag) and could
    silently contradict each other in the same report.

    Preference order:
      1. Total Float <= 0 — the standard CPM definition; float is numeric
         and directly measures schedule slack, so it's authoritative
         whenever the column is present and populated.
      2. The source file's own 'Critical ?' flag — used only as a fallback
         when Total Float isn't present in this particular export, so the
         signal degrades gracefully rather than reporting "no critical
         tasks" on a plan that simply doesn't carry a float column.
    """
    if "total_float" in tasks.columns and tasks["total_float"].notna().any():
        return pd.to_numeric(tasks["total_float"], errors="coerce").fillna(999) <= 0
    if "critical" in tasks.columns:
        return tasks["critical"].fillna(False)
    return pd.Series(False, index=tasks.index)


def _score_schedule_slippage(tasks: pd.DataFrame) -> dict:
    active = tasks[~tasks.get("on_hold", pd.Series(False, index=tasks.index)).fillna(False)]
    active = active[~active.get("not_applicable", pd.Series(False, index=tasks.index)).fillna(False)]
    variances = active["variance_days"].dropna() if "variance_days" in active else pd.Series(dtype=float)

    if variances.empty:
        return {"score": 1, "detail": "No variance data available for active tasks.", "worst_variance_days": None, "zero_float_slip": False, "slipping_tasks": []}

    worst_variance = variances.min()  # most negative = most behind
    worst_row = cast(pd.Series, active.loc[variances.idxmin()])
    zero_float_slip = False
    if "variance_days" in active.columns:
        critical_rows = active[_critical_mask(active)]
        if not critical_rows.empty:
            zf_variance = critical_rows["variance_days"].dropna()
            if not zf_variance.empty and zf_variance.min() < 0:
                zero_float_slip = True

    if worst_variance >= 0:
        score = 0
    elif zero_float_slip:
        score = 2
    elif worst_variance >= -5:
        score = 1
    else:
        score = 2

    # Up to 3 worst-slipping active tasks, named, for the docx table / pptx /
    # LLM prompt — not just a single aggregate day count.
    slipping = active.loc[variances[variances < 0].sort_values().index[:3]]
    slipping_tasks = [
        _task_detail(row, {"variance_days": float(row["variance_days"])})
        for _, row in slipping.iterrows()
    ]

    return {
        "score": score,
        "detail": f"Worst task-level schedule variance is {worst_variance:.0f} day(s) on "
                  f"\"{_task_label(worst_row)}\""
                   f"{' — a zero-float (critical) task' if zero_float_slip else ''}.",
        "worst_variance_days": float(worst_variance),
        "worst_variance_task": _task_detail(worst_row, {"variance_days": float(worst_variance)}),
        "zero_float_slip": zero_float_slip,
        "slipping_tasks": slipping_tasks,
    }


def _score_progress_vs_plan(summary: dict) -> dict:
    pct_complete = summary.get("% Complete")
    start = _to_date(summary.get("Project Start Date"))
    end = _to_date(summary.get("Project End Date"))
    today = _to_date(summary.get("Today's Date")) or datetime.utcnow()

    if pct_complete is None or start is None or end is None or end <= start:
        return {"score": 1, "detail": "Insufficient date/progress data to compare plan vs actual.",
                "pct_complete": pct_complete, "pct_time_elapsed": None}

    total_days = (end - start).days
    elapsed_days = max(0, min((today - start).days, total_days))
    pct_time_elapsed = elapsed_days / total_days if total_days else 0
    gap_pts = (pct_time_elapsed - pct_complete) * 100

    if gap_pts <= 0:
        score = 0
    elif gap_pts <= 15:
        score = 1
    else:
        score = 2

    return {
        "score": score,
        "detail": f"{pct_complete*100:.0f}% complete vs {pct_time_elapsed*100:.0f}% of planned duration elapsed "
                  f"({gap_pts:.0f} pt gap).",
        "pct_complete": pct_complete,
        "pct_time_elapsed": round(pct_time_elapsed, 3),
    }


def _score_blockers(tasks: pd.DataFrame, comments_log: list) -> dict:
    open_blockers = []       # raw text, kept for backward-compat consumers
    blocker_details = []     # {task_name, owner, text} — named where possible

    for col in ("status_comment", "comments"):
        if col in tasks.columns:
            for idx, val in tasks[col].dropna().items():
                text = str(val)
                if is_blocker_text(text):
                    open_blockers.append(text)
                    row = cast(pd.Series, tasks.loc[cast(Any, idx)])
                    blocker_details.append({
                        "task_name": _clean(row.get("task_name")) or "Unnamed task",
                        "owner": _clean(row.get("owner")) or _clean(row.get("assigned_to")),
                        "text": text,
                    })

    for entry in comments_log:
        text = str(entry.get("comment") or "")
        if is_blocker_text(text):
            open_blockers.append(entry.get("comment"))
            blocker_details.append({
                "task_name": None,  # comments_log is a free-form PM log, not tied to a task row
                "owner": entry.get("author"),
                "text": entry.get("comment"),
            })

    # crude recency proxy: comments log has timestamps; task-level comments don't,
    # so age is only computed where we have a timestamp.
    old_blocker = False
    for entry in comments_log:
        text = str(entry.get("comment") or "")
        ts = entry.get("timestamp")
        if is_blocker_text(text) and isinstance(ts, str):
            try:
                ts_date = datetime.strptime(ts, "%m/%d/%y %I:%M %p")
                if (datetime.utcnow() - ts_date).days >= 14:
                    old_blocker = True
            except ValueError:
                pass

    n = len(open_blockers)
    if n == 0:
        score = 0
    elif old_blocker or n >= 4:
        score = 2
    else:
        score = 1

    return {
        "score": score,
        "detail": f"{n} open blocker/friction reference(s) found in comments"
                  f"{' with at least one open 14+ days' if old_blocker else ''}.",
        "count": n,
        "old_blocker_present": old_blocker,
        "examples": open_blockers[:3],
        "blocker_details": blocker_details[:5],
    }


def _score_sentiment(tasks: pd.DataFrame, comments_log: list) -> dict:
    """Deterministic, exemplar-similarity fallback (see semantic_signals.py).
    When an LLM provider is configured, graph.py overrides this with a
    model-assessed sentiment score derived from the same underlying text —
    this function guarantees the pipeline still produces a defensible
    sentiment signal with no API key."""
    texts = []
    for col in ("status_comment", "comments"):
        if col in tasks.columns:
            texts += [str(v) for v in tasks[col].dropna()]
    texts += [str(e.get("comment")) for e in comments_log if e.get("comment")]

    if not texts:
        return {"score": 0, "detail": "No stakeholder comments available; assuming neutral.", "method": "fallback_semantic"}

    hits = sum(1 for t in texts if is_negative_sentiment_text(t))
    ratio = hits / len(texts)
    if ratio == 0:
        score = 0
    elif ratio < 0.3:
        score = 1
    else:
        score = 2

    return {
        "score": score,
        "detail": f"{hits}/{len(texts)} comments contain friction/escalation language.",
        "method": "fallback_semantic",
    }


def _score_critical_path(tasks: pd.DataFrame) -> dict:
    has_float = "total_float" in tasks.columns and tasks["total_float"].notna().any()
    has_flag = "critical" in tasks.columns
    if (not has_float and not has_flag) or "variance_days" not in tasks.columns:
        return {"score": 1, "detail": "Critical-path flag not available in source data.", "at_risk_critical_tasks": None, "at_risk_critical_task_details": []}

    # Uses the same _critical_mask() as _score_schedule_slippage's zero-float
    # override, so this signal's "N critical-path task(s) slipping" count can
    # never contradict the schedule_slippage signal's "zero-float task
    # slipping" flag in the same report.
    critical = tasks[_critical_mask(tasks)]
    at_risk_critical = critical[critical["variance_days"].fillna(0) < 0]
    n = len(at_risk_critical)

    if n == 0:
        score = 0
    elif n == 1:
        score = 1
    else:
        score = 2

    at_risk_details = [
        _task_detail(row, {"variance_days": float(row["variance_days"])})
        for _, row in at_risk_critical.sort_values("variance_days").head(5).iterrows()
    ]
    names = ", ".join(f"\"{d['task_name']}\"" for d in at_risk_details) if at_risk_details else ""

    return {
        "score": score,
        "detail": f"{n} critical-path task(s) currently slipping" + (f": {names}." if names else "."),
        "at_risk_critical_tasks": n,
        "at_risk_critical_task_details": at_risk_details,
    }


def _score_data_completeness(completeness: float) -> dict:
    if completeness >= 0.95:
        score = 0
    elif completeness >= 0.80:
        score = 1
    else:
        score = 2
    return {"score": score, "detail": f"{completeness*100:.0f}% of core fields populated.", "completeness": completeness}


def compute_signals(project: dict) -> dict:
    """Compute all six weighted signals for a loaded, cleaned project dict
    (as returned by data_loader.load_project_plan)."""
    tasks = project["tasks"]
    summary = project["summary"]
    comments_log = project["comments_log"]
    completeness = project["data_completeness"]

    return {
        "schedule_slippage": _score_schedule_slippage(tasks),
        "progress_vs_plan": _score_progress_vs_plan(summary),
        "blockers": _score_blockers(tasks, comments_log),
        "stakeholder_sentiment": _score_sentiment(tasks, comments_log),
        "critical_path_exposure": _score_critical_path(tasks),
        "data_completeness": _score_data_completeness(completeness),
    }


def compute_composite(signals: dict) -> dict:
    """Weighted composite score (0-100) + band, per Phase 1 methodology.
    A signal score of 0/1/2 is converted to a 0-100 sub-score (0, 50, 100)
    before weighting, so the composite lands on an intuitive 0-100 scale."""
    total = 0.0
    breakdown = {}
    for signal, weight in SIGNAL_WEIGHTS.items():
        raw_score = signals[signal]["score"]  # 0, 1, or 2
        sub_score_pct = (2 - raw_score) / 2 * 100  # 0->100, 1->50, 2->0
        contribution = sub_score_pct * weight / 100
        total += contribution
        breakdown[signal] = {
            "raw_score": raw_score,
            "sub_score_pct": sub_score_pct,
            "weight": weight,
            "contribution": round(contribution, 2),
        }

    composite = round(total, 1)
    if composite >= BAND_THRESHOLDS["green_min"]:
        band = "Green"
    elif composite >= BAND_THRESHOLDS["amber_min"]:
        band = "Amber"
    else:
        band = "Red"

    return {"composite_score": composite, "band": band, "breakdown": breakdown}


def apply_overrides(composite_result: dict, signals: dict, source_rag: str | None = None) -> dict:
    """Apply hard override rules that can force a status regardless of the
    composite score. Returns the final band plus a list of triggered rules
    and any human-review flags (e.g. disagreement with the source file's
    own Schedule Health / RAG column)."""
    band = composite_result["band"]
    triggered = []

    if signals["blockers"]["old_blocker_present"]:
        band = "Red"
        triggered.append(f"Blocker open >= {OVERRIDE_RULES['blocker_age_force_red_days']} days -> forced RED")

    if signals["schedule_slippage"]["zero_float_slip"]:
        band = "Red"
        triggered.append("Zero-float critical task slipping -> forced RED")

    completeness = signals["data_completeness"]["completeness"]
    if completeness < OVERRIDE_RULES["data_completeness_amber_cap"]:
        if band == "Green":
            band = "Amber"
        triggered.append(f"Data completeness {completeness*100:.0f}% < 80% -> capped at AMBER (low confidence)")

    disagreement = None
    if source_rag:
        order = {"Green": 0, "Amber": 1, "Yellow": 1, "Red": 2}
        computed_idx = order.get(band)
        source_idx = order.get(source_rag)
        if computed_idx is not None and source_idx is not None and abs(computed_idx - source_idx) > OVERRIDE_RULES["disagreement_band_gap_for_review"]:
            disagreement = {
                "source_value": source_rag,
                "computed_value": band,
                "note": "Computed status disagrees with source file's existing RAG/Schedule Health by more than one band - flagged for PM review, not auto-resolved.",
            }
        elif computed_idx is not None and source_idx is not None and computed_idx != source_idx:
            disagreement = {
                "source_value": source_rag,
                "computed_value": band,
                "note": "Computed status differs from source file's existing RAG/Schedule Health.",
            }

    return {"final_band": band, "overrides_triggered": triggered, "disagreement": disagreement}
