"""
Project Health Reporting Agent — Streamlit frontend.

Home page: portfolio-level dashboard. This app is a pure client of the
FastAPI backend (`project_health_agent.api.main`) — no scoring, file
parsing, or LLM calls happen here; every number on screen was already
computed by Phase 2/3 and is just being fetched and displayed.

Run:
    streamlit run frontend/app.py
(with the API running separately: `uvicorn project_health_agent.api.main:app --reload`)
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from api_client import ApiError, cached_projects, get, render_api_status_sidebar

st.set_page_config(
    page_title="Project Health Agent",
    page_icon="\U0001F4CA",
    layout="wide",
)

RAG_COLORS = {"Green": "#1E8E3E", "Amber": "#F9A825", "Red": "#D93025"}

render_api_status_sidebar()

st.title("Project Health Reporting Agent")
st.caption(
    "AI agent that reads project plan exports, independently determines a "
    "RAG status with plain-English reasoning, and synthesizes an executive "
    "portfolio review across the whole book of work."
)

try:
    projects = cached_projects()
except ApiError as exc:
    st.error(str(exc))
    st.stop()

if not projects:
    st.info(
        "No weekly reports on disk yet. Go to **Run Agent** in the sidebar "
        "to score your first project plan(s)."
    )
    st.stop()

# --- Try to load the latest monthly portfolio package for a richer view ----
package = None
try:
    package = get("/monthly/package")
except ApiError:
    pass

col1, col2, col3, col4 = st.columns(4)
col1.metric("Projects tracked", len(projects))
total_runs = sum(int(p["runs"]) for p in projects)
col2.metric("Weekly runs on file", total_runs)

if package:
    band_mix = package.get("band_mix", {})
    col3.metric("Green", band_mix.get("Green", 0))
    col4.metric("Red", band_mix.get("Red", 0))
else:
    col3.metric("Green", "—")
    col4.metric("Red", "—")
    st.warning(
        "No monthly portfolio package yet. Go to **Monthly Synthesis** in "
        "the sidebar to aggregate the projects above into a portfolio view "
        "and executive deck.",
        icon="ℹ️",
    )

st.divider()

if package:
    left, right = st.columns([1, 2])

    with left:
        st.subheader("RAG mix")
        band_mix = package.get("band_mix", {})
        if band_mix:
            fig = px.pie(
                names=list(band_mix.keys()),
                values=list(band_mix.values()),
                color=list(band_mix.keys()),
                color_discrete_map=RAG_COLORS,
                hole=0.45,
            )
            fig.update_traces(textinfo="value+label")
            fig.update_layout(showlegend=False, margin=dict(t=10, b=10, l=10, r=10), height=300)
            st.plotly_chart(fig, use_container_width=True)

    with right:
        st.subheader("Composite score by project")
        proj_rows = package.get("projects", [])
        if proj_rows:
            df = pd.DataFrame(proj_rows)[["project_name", "current_rag", "composite_score"]]
            fig2 = px.bar(
                df.sort_values("composite_score"),
                x="composite_score",
                y="project_name",
                color="current_rag",
                color_discrete_map=RAG_COLORS,
                orientation="h",
                labels={"composite_score": "Composite score", "project_name": "", "current_rag": "RAG"},
            )
            fig2.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=300)
            st.plotly_chart(fig2, use_container_width=True)

    st.subheader("Recurring themes & emerging risks")
    themes = package.get("recurring_themes", [])
    if themes:
        st.write("**Recurring themes across projects:** " + ", ".join(themes))

    risks = package.get("emerging_risks", [])
    if risks:
        for risk in risks:
            severity = risk.get("severity", "")
            icon = {"Critical": "🔴", "High": "🟠", "Medium": "🟡"}.get(severity, "⚪")
            with st.expander(f"{icon} [{severity}] {risk.get('category', '')}"):
                st.write(risk.get("statement", ""))
                if risk.get("affected_projects"):
                    st.caption("Affected: " + ", ".join(risk["affected_projects"]))
    else:
        st.caption("No emerging risks flagged in the latest monthly run.")

st.divider()
st.subheader("All tracked projects")
df_projects = pd.DataFrame(projects)
st.dataframe(df_projects, use_container_width=True, hide_index=True)
