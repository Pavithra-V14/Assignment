"""
The Phase 2 agent, built as a LangGraph StateGraph.

Node design (see README.md for the full rationale):
  ingest        -> load + clean the project plan (deterministic)
  score         -> compute the six weighted signals + composite + overrides (deterministic)
  reason        -> ONE LLM call: reasons over the computed evidence, writes
                    plain-English drivers + narrative, and states its own
                    RAG opinion for cross-checking against the deterministic
                    band (deterministic scoring remains authoritative; a
                    disagreement here is surfaced, never silently resolved)
  finalize      -> assembles the weekly report JSON + markdown

Classification and reasoning are deliberately ONE LLM call, not two — see
README "Design decisions" for why splitting them adds cost/latency without
improving faithfulness (a separate "classifier" and "explainer" call risks
the explainer post-hoc rationalizing a status it did not itself decide).
"""
import json
from datetime import UTC, datetime
from typing import TypedDict

from langgraph.graph import END, StateGraph

from project_health_agent.ingestion.data_loader import load_project_plan
from project_health_agent.reasoning.llm_client import generate_json
from project_health_agent.scoring.metrics import apply_overrides, compute_composite, compute_signals


class WeeklyState(TypedDict, total=False):
    source_path: str
    project: dict
    signals: dict
    composite: dict
    overrides: dict
    llm_result: dict
    report: dict


SYSTEM_PROMPT = """You are a Project Health Reporting Agent for a professional services delivery team.
You are given deterministic, already-computed evidence about one project: six weighted signal
scores (schedule slippage, progress vs plan, blockers, stakeholder sentiment, critical path
exposure, data completeness), a composite score, a computed RAG band, and any override rules that
already fired. Several signals also carry named task-level evidence (task name, owner, phase, and
variance) — e.g. worst_variance_task, slipping_tasks, at_risk_critical_task_details,
blocker_details. You are also given raw PM/status comments.

Your job is NOT to re-decide the RAG band from scratch — the composite score and override rules
are authoritative and were computed deterministically for auditability. Your job is to:
1. Independently state what RAG status you would assign given the same evidence (for cross-check
   purposes only - if you disagree with the computed band, say so honestly, don't just agree).
2. Identify the 2-4 factors that most drove this week's status. Be concrete and specific: name the
   actual task(s) involved (not "a critical task" but the task's real name from the evidence, e.g.
   "JDE Mapping Workshop"), cite its owner if given, and cite exact dates/day counts/comments —
   never a vague generality when a named task or comment is available in the evidence.
3. Write a short plain-English narrative (3-5 sentences) a Project Manager or VP could read without
   needing to know what a "composite score" is — again naming the specific task(s) and issue(s)
   driving the status, not just restating the RAG color.
4. Recommend 2-3 concrete next actions, each tied to a named task or issue where the evidence
   supports it (e.g. "Re-sequence 'JDE Mapping Workshop' or add a resource" rather than "review the
   schedule").
5. Rate stakeholder sentiment (0=neutral/positive, 1=mild concern, 2=escalation/frustration) from
   the actual tone of the comments provided, independent of the keyword-based fallback score you
   were given, and briefly say why.

Respond with ONLY a JSON object, no prose outside it, matching this schema:
{
  "llm_rag_opinion": "Green" | "Amber" | "Red",
  "llm_sentiment_score": 0 | 1 | 2,
  "sentiment_reasoning": "...",
  "top_drivers": ["...", "...", "..."],
  "plain_english_reasoning": "...",
  "recommended_actions": ["...", "...", "..."]
}"""


def _build_user_prompt(state: WeeklyState) -> str:
    project = state["project"]
    payload = {
        "project_name": project["project_name"],
        "summary": {k: str(v) for k, v in project["summary"].items()},
        "computed_composite_score": state["composite"]["composite_score"],
        "computed_band": state["composite"]["band"],
        "final_band_after_overrides": state["overrides"]["final_band"],
        "overrides_triggered": state["overrides"]["overrides_triggered"],
        # Full per-signal evidence (score/detail + any named task-level
        # fields like worst_variance_task/slipping_tasks/blocker_details) —
        # trimming this to just {score, detail} previously starved the model
        # of the task names it needed to be specific instead of generic.
        "signal_breakdown": state["signals"],
        "raw_comments_sample": [
            str(v) for v in project["tasks"].get("status_comment", []).dropna().tolist()[:10]
        ] + [str(v) for v in project["tasks"].get("comments", []).dropna().tolist()[:10]]
          + [c.get("comment") for c in project["comments_log"][:10]],
    }
    return json.dumps(payload, indent=2, default=str)


def _fallback_reasoning(state: WeeklyState) -> dict:
    """Deterministic, template-based stand-in for the LLM reasoning step.
    Used automatically when no API key is configured, or if the live call
    fails, so the pipeline always produces a complete weekly report."""
    signals = state["signals"]
    band = state["overrides"]["final_band"]

    ranked = sorted(signals.items(), key=lambda kv: kv[1]["score"], reverse=True)
    top_drivers = []
    for name, info in ranked[:3]:
        if info["score"] == 0:
            continue
        detail = info["detail"]
        # Blockers' own "detail" string is a count, not a named task (unlike
        # schedule_slippage/critical_path_exposure, which already name the
        # task) — so append the specific task/comment here for parity.
        if name == "blockers" and info.get("blocker_details"):
            named = [
                f"\"{b['task_name']}\"" if b.get("task_name") else "an unassigned task"
                for b in info["blocker_details"][:2]
            ]
            detail += f" Affected: {', '.join(named)}."
        top_drivers.append(f"{name.replace('_', ' ').title()}: {detail}")
    if not top_drivers:
        top_drivers = ["All signals within healthy range this week."]

    narrative = (
        f"This project is currently {band} based on {len(top_drivers)} key factor(s): "
        + " ".join(top_drivers[:2])
        + (" Recommend continued monitoring; no immediate escalation required." if band == "Green"
           else " Recommend PM follow-up this week to prevent further slippage." if band == "Amber"
           else " Recommend immediate escalation to the delivery lead and client-facing communication.")
    )

    actions = {
        "Green": ["Continue current cadence.", "Re-confirm upcoming milestone owners."],
        "Amber": ["PM to review at-risk tasks with owners this week.", "Re-baseline if slippage continues next cycle."],
        "Red": ["Escalate to delivery leadership immediately.", "Convene a recovery-plan session with the client.", "Re-sequence or add resources to the blocking critical-path task(s)."],
    }[band]

    # Name the actual critical-path task(s) in the Red recovery action, when
    # the evidence has one, instead of the generic "critical-path task(s)".
    cp_details = signals.get("critical_path_exposure", {}).get("at_risk_critical_task_details") or []
    if band == "Red" and cp_details:
        names = ", ".join(f"\"{d['task_name']}\"" for d in cp_details[:2])
        actions = actions[:-1] + [f"Re-sequence or add resources to: {names}."]

    return {
        "llm_rag_opinion": band,
        "llm_sentiment_score": signals["stakeholder_sentiment"]["score"],
        "sentiment_reasoning": "Derived from keyword-based fallback scan (no LLM provider configured).",
        "top_drivers": top_drivers,
        "plain_english_reasoning": narrative,
        "recommended_actions": actions,
    }


def node_ingest(state: WeeklyState) -> WeeklyState:
    project = load_project_plan(state["source_path"])
    return {"project": project}


def node_score(state: WeeklyState) -> WeeklyState:
    signals = compute_signals(state["project"])
    composite = compute_composite(signals)
    source_rag = state["project"]["summary"].get("Schedule Health")
    overrides = apply_overrides(composite, signals, source_rag)
    return {"signals": signals, "composite": composite, "overrides": overrides}


def node_reason(state: WeeklyState) -> WeeklyState:
    user_prompt = _build_user_prompt(state)
    llm_result = generate_json(SYSTEM_PROMPT, user_prompt, lambda: _fallback_reasoning(state))
    return {"llm_result": llm_result}


def node_finalize(state: WeeklyState) -> WeeklyState:
    project = state["project"]
    overrides = state["overrides"]
    llm_result = state["llm_result"]

    model_disagrees = llm_result.get("llm_rag_opinion") != overrides["final_band"]

    report = {
        "project_name": project["project_name"],
        "source_path": state["source_path"],
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "final_rag_status": overrides["final_band"],
        "composite_score": state["composite"]["composite_score"],
        "raw_band_before_overrides": state["composite"]["band"],
        "overrides_triggered": overrides["overrides_triggered"],
        "source_file_rag_label": project["summary"].get("Schedule Health"),
        "source_vs_computed_disagreement": overrides["disagreement"],
        "llm_rag_opinion": llm_result.get("llm_rag_opinion"),
        "model_vs_deterministic_disagreement": (
            f"LLM independently suggested {llm_result.get('llm_rag_opinion')} vs computed "
            f"{overrides['final_band']} — flagged for review, deterministic score remains authoritative."
            if model_disagrees else None
        ),
        # Keep the FULL per-signal evidence dict (not just score/detail).
        # aggregator.py's portfolio-level risk detection (critical-path
        # concentration, aged-blocker exposure, etc.) reads fields like
        # at_risk_critical_tasks / old_blocker_present straight out of this
        # structure — trimming it here previously caused those fields to
        # silently read as None/False downstream with no error raised.
        "signal_breakdown": {
            name: info for name, info in state["signals"].items()
        },
        "top_drivers": llm_result.get("top_drivers"),
        "plain_english_reasoning": llm_result.get("plain_english_reasoning"),
        "recommended_actions": llm_result.get("recommended_actions"),
        "stakeholder_sentiment": {
            "score": llm_result.get("llm_sentiment_score"),
            "reasoning": llm_result.get("sentiment_reasoning"),
        },
        "data_completeness": project["data_completeness"],
        "generated_by": llm_result.get("generated_by"),
        "fallback_reason": llm_result.get("fallback_reason"),
    }
    return {"report": report}


def build_graph():
    graph = StateGraph(WeeklyState)
    graph.add_node("ingest", node_ingest)
    graph.add_node("score", node_score)
    graph.add_node("reason", node_reason)
    graph.add_node("finalize", node_finalize)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "score")
    graph.add_edge("score", "reason")
    graph.add_edge("reason", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()


def run_weekly_agent(source_path: str) -> dict:
    app = build_graph()
    final_state = app.invoke({"source_path": source_path})
    return final_state["report"]
