"""
Phase 3, step 2: portfolio-level synthesis.

Takes the deterministic portfolio package (aggregator.py) and makes ONE LLM
call that must return slide-ready structured JSON — not prose. This is the
key design choice for reliable deck generation: the model decides *what the
story is* (which trends matter, what's an emerging risk, what a VP should
do about it), and deck_builder.py deterministically renders whatever JSON
comes back. If the call fails or no provider is configured, a template
fallback builds an equivalent-shape deck from the same aggregated data, so
`run_monthly.py` always produces a complete deck.
"""
import json

from project_health_agent.reasoning.llm_client import generate_json

SYSTEM_PROMPT = """You are preparing a monthly executive presentation for a VP of Professional Services
Delivery, to be shown to leadership and possibly a client with minimal edits. You are given a
portfolio package: current RAG status for each project, score trends (if enough history exists),
top drivers per project, and recurring themes/risks already detected across projects.

Do NOT simply summarize each project one by one — the brief explicitly asks for cross-project
trends, emerging risks, and executive-level recommendations, not a status recap. Identify what a
VP actually needs to decide or act on this month (staffing, client escalation, scope
renegotiation, process fixes), not project trivia.

Return ONLY a JSON object with exactly 5 slides, in this exact shape:
{
  "slides": [
    {
      "layout": "title",
      "title": "...",
      "subtitle": "..."
    },
    {
      "layout": "bullets_with_chart",
      "title": "...",
      "chart_type": "rag_pie",
      "bullets": ["...", "..."]
    },
    {
      "layout": "bullets",
      "title": "Project-by-Project Snapshot",
      "bullets": ["...", "...", "..."]
    },
    {
      "layout": "risk_items",
      "title": "Emerging Risks & Cross-Project Themes",
      "items": [
        {"category": "...", "severity": "Critical|High|Medium", "statement": "...", "affected_projects": ["..."], "affected_tasks": ["..."], "metric": "..."}
      ]
    },
    {
      "layout": "recommendation_items",
      "title": "Recommendations & Decisions Needed",
      "items": [
        {"priority": "P1|P2|P3", "action": "...", "owner": "...", "project": "...", "rationale": "..."}
      ]
    }
  ]
}

Rules:
- Slide 1 is always layout "title" (deck title + subtitle covering the reporting period).
- Exactly one slide should use "chart_type": "rag_pie" (portfolio RAG mix) — use layout
  "bullets_with_chart" for it, and still include 2-3 bullets of interpretation, not just the chart.
- Slide 3 uses layout "bullets", one line per project with status/score/top driver.
- Slide 4 (layout "risk_items") must classify each risk into a category (e.g. "Critical-Path
  Concentration", "Data Confidence", "Governance / Reporting Integrity", "Trend Deterioration",
  "Recurring Delivery Theme") with a severity and the specific projects/metric behind it — do not
  emit generic prose bullets for this slide. Each project in the input carries named task-level
  evidence (at_risk_task_names, worst_slipping_task) — use those actual task names in the
  statement/affected_tasks fields wherever a risk traces back to specific tasks, instead of saying
  "a critical task" or "several tasks".
- Slide 5 (layout "recommendation_items") must assign each action a priority (P1 = act this week,
  P2 = resolve before next client touchpoint, P3 = process/systemic fix), an owner role (e.g.
  "Delivery Lead", "PMO / Governance", "Client Partner", "Program Manager"), and a one-line
  rationale tied to the evidence — not vague advice. Name the specific task(s) in the rationale
  when the underlying project data provides one (at_risk_task_names / worst_slipping_task).
- Prefer portfolio-wide/systemic recommendations over repeating the same per-project escalation
  when a risk affects 2+ projects (e.g. one "institute a critical-path review cadence" action
  rather than N separate "fix the schedule" actions).
- Everything must be grounded in the data provided — do not invent projects, numbers, or risks not
  present in the input. Avoid jargon like "composite score" or "override rule" in prose fields;
  translate into business terms.
"""


def _build_recommendations(package: dict) -> list:
    """Structured, prioritized recommendations (P1 = act this week, P2 =
    resolve before the next client touchpoint, P3 = process/systemic fix).
    Driven off portfolio_signals + the structured risk categories from
    aggregator.py, not just a per-project Red check — so a systemic issue
    (e.g. critical-path slippage on 2+ projects, or a data-confidence gap)
    produces one portfolio-wide action instead of being silently absorbed
    into per-project escalations."""
    signals = package.get("portfolio_signals", {})
    risk_by_category = {r["category"]: r for r in package.get("emerging_risks", [])}
    items = []

    for p in package["projects"]:
        if p["current_rag"] == "Red":
            top_driver = (p.get("top_drivers") or ["schedule and delivery signals"])[0]
            items.append({
                "priority": "P1",
                "action": f"Escalate {p['project_name']} to delivery leadership this week.",
                "owner": "Delivery Lead",
                "project": p["project_name"],
                "rationale": top_driver,
            })

    governance_risk = risk_by_category.get("Governance / Reporting Integrity")
    if governance_risk:
        priority = "P1" if governance_risk["severity"] == "High" else "P2"
        for name in governance_risk["affected_projects"]:
            items.append({
                "priority": priority,
                "action": f"Reconcile PM-reported vs. computed status on {name} before any client-facing "
                          f"communication.",
                "owner": "PMO / Governance",
                "project": name,
                "rationale": "Source file status disagrees with independently computed RAG by more than one band.",
            })

    cp_risk = risk_by_category.get("Critical-Path Concentration")
    if cp_risk:
        items.append({
            "priority": "P2",
            "action": "Institute a portfolio-wide critical-path review cadence (weekly, cross-project) "
                      "rather than handling each slipping task in isolation.",
            "owner": "PMO / Governance",
            "project": "Portfolio-wide",
            "rationale": cp_risk["statement"],
        })

    data_risk = risk_by_category.get("Data Confidence")
    if data_risk:
        items.append({
            "priority": "P3",
            "action": "Request updated baseline/variance fields from delivery teams on projects below the "
                      "data-completeness floor; low-confidence status should not be treated as final.",
            "owner": "PMO / Governance",
            "project": ", ".join(data_risk["affected_projects"]),
            "rationale": data_risk["statement"],
        })

    theme_risk = risk_by_category.get("Recurring Delivery Theme")
    if theme_risk:
        theme_name = theme_risk.get("theme", "shared root-cause")
        items.append({
            "priority": "P3",
            "action": f"Root-cause '{theme_name}' at the program level instead of remediating it "
                      f"project-by-project.",
            "owner": "Program Manager",
            "project": ", ".join(theme_risk["affected_projects"]),
            "rationale": theme_risk["statement"],
        })

    if not items:
        items.append({
            "priority": "P3",
            "action": "Maintain current delivery cadence across the portfolio; no immediate escalations required.",
            "owner": "PMO / Governance",
            "project": "Portfolio-wide",
            "rationale": f"Portfolio average composite score is {signals.get('avg_composite_score', 'n/a')}/100 "
                         f"with no Red-flagged engagements.",
        })

    priority_rank = {"P1": 0, "P2": 1, "P3": 2}
    items.sort(key=lambda r: priority_rank.get(r["priority"], 9))
    return items


def _fallback_slides(package: dict) -> dict:
    band_mix = package["band_mix"]
    total = sum(band_mix.values())
    mix_line = ", ".join(f"{count} {band}" for band, count in band_mix.items())

    risk_items = package["emerging_risks"][:6]

    project_bullets = []
    for p in package["projects"]:
        drivers = "; ".join((p.get("top_drivers") or [])[:1])
        project_bullets.append(f"{p['project_name']}: {p['current_rag']} ({p['composite_score']}/100) - {drivers}")

    recommendation_items = _build_recommendations(package)

    return {
        "slides": [
            {"layout": "title", "title": "Portfolio Health Review",
             "subtitle": f"Monthly synthesis across {total} active engagement(s)"},
            {"layout": "bullets_with_chart", "title": "Portfolio RAG Overview", "chart_type": "rag_pie",
             "bullets": [f"Current mix: {mix_line}.",
                         "Status reflects schedule, milestone, and blocker health; financial/budget health is out of scope for this data set."]},
            {"layout": "bullets", "title": "Project-by-Project Snapshot", "bullets": project_bullets},
            {"layout": "risk_items", "title": "Emerging Risks & Cross-Project Themes", "items": risk_items},
            {"layout": "recommendation_items", "title": "Recommendations & Decisions Needed", "items": recommendation_items},
        ]
    }


def synthesize_portfolio(package: dict) -> dict:
    user_prompt = json.dumps(package, indent=2, default=str)
    result = generate_json(SYSTEM_PROMPT, user_prompt, lambda: _fallback_slides(package))
    return result
