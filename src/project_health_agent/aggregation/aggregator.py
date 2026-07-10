"""
Phase 3, step 1: deterministic aggregation across all weekly agent outputs.

Reads every JSON file under outputs/weekly/<project>/*.json (Phase 2's
output — this module never touches the raw Excel files), and produces a
compact "portfolio package": current status mix, per-project history and
trend direction (once >=2 weekly runs exist per project), and recurring
driver/blocker themes across projects. This package is what gets handed to
the synthesis LLM call in synthesis_agent.py — trend computation itself is
NOT delegated to the model, for the same auditability reason as Phase 2's
scoring.

Note: with only one weekly snapshot per sample project available in this
assignment, per-project time-trend fields will show "insufficient history"
until the weekly agent has run >=2 times for that project. Cross-project
comparison (today's status mix, shared themes) works from a single snapshot
and is what's demonstrated in the bundled monthly run.
"""
import glob
import json
import os
from collections import Counter

from project_health_agent.core.config import settings
from project_health_agent.core.exceptions import ProjectHealthAgentError
from project_health_agent.core.logging_config import get_logger
from project_health_agent.scoring.semantic_signals import match_themes

logger = get_logger("aggregation.aggregator")


def _load_project_history(project_dir: str, year_month: str | None = None) -> list:
    """Load weekly report JSONs for one project, oldest->newest.

    Weekly files are named ``<week_ending YYYY-MM-DD>.json`` (see
    cli/weekly.py), so filtering to a single calendar month is a plain
    filename-prefix match — no need to open/parse every file just to
    filter it out.
    """
    files = sorted(glob.glob(os.path.join(project_dir, "*.json")))
    if year_month:
        files = [f for f in files if os.path.basename(f).startswith(year_month)]
    history = []
    for f in files:
        with open(f) as fh:
            history.append(json.load(fh))
    return history


def load_all_project_histories(year_month: str | None = None) -> dict:
    """{project_slug: [report_week1, report_week2, ...]} sorted oldest->newest.

    year_month: optional "YYYY-MM" filter. When given, only weekly runs
    whose week_ending falls in that calendar month are included — this is
    what makes a monthly report a *monthly* report instead of "every
    weekly run ever produced". When omitted, every run on disk is
    returned (used by the weekly-browsing endpoints/pages, which are
    intentionally unscoped).
    """
    histories: dict[str, list] = {}
    if not os.path.isdir(settings.WEEKLY_OUTPUT_DIR):
        return histories
    for project_dir in sorted(glob.glob(os.path.join(settings.WEEKLY_OUTPUT_DIR, "*"))):
        if os.path.isdir(project_dir):
            slug = os.path.basename(project_dir)
            history = _load_project_history(project_dir, year_month=year_month)
            if history:
                histories[slug] = history
    return histories


def list_available_months() -> list[str]:
    """Every "YYYY-MM" that has at least one weekly run, across all
    projects, newest first. Used to populate the month picker in the API
    and the Streamlit UI."""
    months: set[str] = set()
    if not os.path.isdir(settings.WEEKLY_OUTPUT_DIR):
        return []
    for project_dir in glob.glob(os.path.join(settings.WEEKLY_OUTPUT_DIR, "*")):
        if not os.path.isdir(project_dir):
            continue
        for f in glob.glob(os.path.join(project_dir, "*.json")):
            months.add(os.path.basename(f)[:7])
    return sorted(months, reverse=True)


def _band_trend(history: list) -> dict:
    bands = [h["final_rag_status"] for h in history]
    scores = [h["composite_score"] for h in history]
    if len(history) < 2:
        return {
            "status": "insufficient_history",
            "note": f"Only {len(history)} weekly run(s) available; trend requires >=2.",
            "current_band": bands[-1] if bands else None,
            "current_score": scores[-1] if scores else None,
        }
    direction = "improving" if scores[-1] > scores[0] else "declining" if scores[-1] < scores[0] else "flat"
    return {
        "status": "ok",
        "band_sequence": bands,
        "score_sequence": scores,
        "score_delta": round(scores[-1] - scores[0], 1),
        "direction": direction,
        "current_band": bands[-1],
        "current_score": scores[-1],
    }


def _extract_theme_keywords(text: str) -> list:
    """Recurring-theme detection across projects (e.g. 'JDE mapping',
    'workshop', 'sample data'), via BM25 search against a small set of
    natural-language theme exemplars (see semantic_signals.THEME_EXEMPLARS)
    rather than a fixed substring keyword map — so a new phrasing of an
    existing theme is picked up without editing this function; extend the
    exemplar list in semantic_signals.py instead."""
    return match_themes(text)


RISK_SEVERITY_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}

# Portfolio-level thresholds — kept here (not buried in the loop below) so
# they're auditable and tunable the same way SIGNAL_WEIGHTS/BAND_THRESHOLDS
# are in config.py for the per-project scoring engine.
DATA_CONFIDENCE_FLOOR = 0.80              # matches OVERRIDE_RULES["data_completeness_amber_cap"]
CRITICAL_TASK_HIGH_WATERMARK = 5          # portfolio-wide slipping critical tasks -> High severity
GOVERNANCE_DISAGREEMENT_MIN_PROJECTS = 1  # any disagreement is surfaced; 2+ escalates severity


def build_portfolio_package(year_month: str | None = None) -> dict:
    """Aggregate weekly outputs into a portfolio package.

    year_month: "YYYY-MM" to scope the report to a single calendar month
    (each project's *latest run within that month* is treated as "current").
    Omit to fall back to the legacy behaviour of aggregating every weekly
    run ever produced, across all months.
    """
    histories = load_all_project_histories(year_month=year_month)
    if not histories:
        if year_month:
            raise ProjectHealthAgentError(
                f"No weekly outputs found for {year_month}. Run the weekly "
                f"agent for that month first, or pick a different month."
            )
        raise ProjectHealthAgentError("No weekly outputs found. Run run_weekly.py first.")

    projects = []
    theme_counter: Counter = Counter()
    project_theme_map = {}

    for slug, history in histories.items():
        latest = history[-1]
        trend = _band_trend(history)

        themes = set()
        for driver in latest.get("top_drivers") or []:
            themes.update(_extract_theme_keywords(driver))
        for theme in themes:
            theme_counter[theme] += 1
        project_theme_map[latest["project_name"]] = sorted(themes)

        cp_signal = (latest.get("signal_breakdown") or {}).get("critical_path_exposure", {})
        blocker_signal = (latest.get("signal_breakdown") or {}).get("blockers", {})
        sched_signal = (latest.get("signal_breakdown") or {}).get("schedule_slippage", {})

        at_risk_task_names = [
            t["task_name"] for t in (cp_signal.get("at_risk_critical_task_details") or [])
            if t.get("task_name")
        ]

        projects.append({
            "project_name": latest["project_name"],
            "current_rag": latest["final_rag_status"],
            "composite_score": latest["composite_score"],
            "trend": trend,
            "top_drivers": latest.get("top_drivers"),
            "source_vs_computed_disagreement": latest.get("source_vs_computed_disagreement"),
            "data_completeness": latest.get("data_completeness"),
            "at_risk_critical_tasks": cp_signal.get("at_risk_critical_tasks"),
            "at_risk_task_names": at_risk_task_names,
            "worst_slipping_task": (sched_signal.get("worst_variance_task") or {}).get("task_name"),
            "open_blockers": blocker_signal.get("count"),
            "old_blocker_present": blocker_signal.get("old_blocker_present", False),
            "themes": sorted(themes),
        })

    band_mix = Counter(p["current_rag"] for p in projects)
    recurring_themes = [theme for theme, count in theme_counter.items() if count >= 2]

    emerging_risks = _detect_risks(projects, recurring_themes, project_theme_map)
    emerging_risks.sort(key=lambda r: RISK_SEVERITY_RANK.get(r["severity"], 9))

    return {
        "month": year_month,
        "generated_from_projects": len(projects),
        "band_mix": dict(band_mix),
        "projects": projects,
        "recurring_themes": recurring_themes,
        "emerging_risks": emerging_risks,
        "portfolio_signals": _portfolio_signals(projects),
    }


def _portfolio_signals(projects: list) -> dict:
    """Portfolio-wide roll-ups used by both risk detection and, downstream,
    synthesis_agent.py's recommendation logic — computed once, here, so the
    two stay consistent with each other and with the per-project numbers."""
    n = len(projects) or 1
    total_critical_at_risk = sum(p.get("at_risk_critical_tasks") or 0 for p in projects)
    below_confidence_floor = [p["project_name"] for p in projects
                               if (p.get("data_completeness") or 1.0) < DATA_CONFIDENCE_FLOOR]
    disagreements = [p["project_name"] for p in projects if p.get("source_vs_computed_disagreement")]
    aged_blocker_projects = [p["project_name"] for p in projects if p.get("old_blocker_present")]
    return {
        "avg_composite_score": round(sum(p["composite_score"] for p in projects) / n, 1),
        "total_critical_tasks_at_risk": total_critical_at_risk,
        "projects_below_data_confidence_floor": below_confidence_floor,
        "projects_with_status_disagreement": disagreements,
        "projects_with_aged_blockers": aged_blocker_projects,
    }


def _detect_risks(projects: list, recurring_themes: list, project_theme_map: dict) -> list:
    """Multi-category structured risk detection. Each risk carries a
    category, severity, the evidence metric it was derived from, and the
    affected project list — so the executive slide (and any downstream
    consumer) can render/sort/filter by severity instead of parsing prose.

    Categories, in the order they're evaluated:
      1. Trend Deterioration      - not-Red but composite score declining
      2. Recurring Delivery Theme - same root-cause keyword across >=2 projects
      3. Critical-Path Concentration - portfolio-wide slipping critical tasks
      4. Data Confidence           - completeness below the scoring floor
      5. Governance / Reporting Integrity - PM-reported vs computed status gap
    """
    risks = []

    for p in projects:
        if p["current_rag"] != "Red" and p["trend"].get("direction") == "declining":
            risks.append({
                "category": "Trend Deterioration",
                "severity": "Medium",
                "statement": f"{p['project_name']} is currently {p['current_rag']} but its composite "
                             f"score is declining (delta {p['trend'].get('score_delta')} pts since first "
                             f"tracked run) — a leading indicator ahead of a band change.",
                "affected_projects": [p["project_name"]],
                "metric": f"score_delta={p['trend'].get('score_delta')}",
            })

    for theme in recurring_themes:
        affected = [name for name, themes in project_theme_map.items() if theme in themes]
        if len(affected) >= 2:
            any_red = any(p["current_rag"] == "Red" for p in projects if p["project_name"] in affected)
            risks.append({
                "category": "Recurring Delivery Theme",
                "severity": "High" if any_red else "Medium",
                "statement": f"Root cause '{theme}' appears independently across {len(affected)} projects, "
                             f"suggesting a shared process gap rather than isolated project execution issues.",
                "affected_projects": affected,
                "metric": f"projects_affected={len(affected)}",
                "theme": theme,
            })

    cp_projects = [p["project_name"] for p in projects if (p.get("at_risk_critical_tasks") or 0) > 0]
    total_cp = sum(p.get("at_risk_critical_tasks") or 0 for p in projects)
    if len(cp_projects) >= 2:
        severity = "Critical" if total_cp >= CRITICAL_TASK_HIGH_WATERMARK else "High"
        all_task_names = [n for p in projects for n in (p.get("at_risk_task_names") or [])]
        named = ", ".join(f"\"{n}\"" for n in all_task_names[:4]) if all_task_names else ""
        risks.append({
            "category": "Critical-Path Concentration",
            "severity": severity,
            "statement": f"{total_cp} critical-path (zero-float) tasks are currently slipping across "
                         f"{len(cp_projects)} projects simultaneously — portfolio-wide delivery-capacity "
                         f"risk, not a single-project scheduling issue."
                         + (f" Affected tasks include {named}." if named else ""),
            "affected_projects": cp_projects,
            "affected_tasks": all_task_names[:8],
            "metric": f"total_critical_tasks_at_risk={total_cp}",
        })

    low_confidence = [p["project_name"] for p in projects
                       if (p.get("data_completeness") or 1.0) < DATA_CONFIDENCE_FLOOR]
    if low_confidence:
        risks.append({
            "category": "Data Confidence",
            "severity": "High" if len(low_confidence) >= 2 else "Medium",
            "statement": f"Core schedule fields are under {int(DATA_CONFIDENCE_FLOOR * 100)}% populated on "
                         f"{len(low_confidence)} project(s) — computed RAG status on these carries reduced "
                         f"statistical confidence and is capped at Amber-or-worse by design.",
            "affected_projects": low_confidence,
            "metric": f"below_{int(DATA_CONFIDENCE_FLOOR*100)}pct_completeness={len(low_confidence)}",
        })

    disagreements = [p for p in projects if p.get("source_vs_computed_disagreement")]
    if disagreements:
        names = [p["project_name"] for p in disagreements]
        risks.append({
            "category": "Governance / Reporting Integrity",
            "severity": "High" if len(disagreements) >= GOVERNANCE_DISAGREEMENT_MIN_PROJECTS else "Medium",
            "statement": f"PM-reported status disagrees with the independently computed status on "
                         f"{len(names)} project(s) by more than one band — a signal that self-reported "
                         f"status may be optimistic and should not be forwarded to clients unreviewed.",
            "affected_projects": names,
            "metric": f"disagreements={len(names)}",
        })

    return risks


if __name__ == "__main__":
    import pprint
    pprint.pprint(build_portfolio_package())
