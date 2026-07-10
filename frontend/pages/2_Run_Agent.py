"""Trigger the weekly agent — against the configured data source, or an upload."""
from __future__ import annotations

import streamlit as st

from api_client import ApiError, cached_health, post, render_api_status_sidebar

st.set_page_config(page_title="Run Agent", page_icon="\u25B6\ufe0f", layout="wide")
render_api_status_sidebar()

RAG_BADGE = {"Green": "🟢", "Amber": "🟠", "Red": "🔴"}

st.title("Run the Weekly Agent")
st.caption(
    "Score project plan(s) and write a JSON + .docx report per project. "
    "This calls the same code path as the scheduled CLI run."
)

health = cached_health()
data_source = health["data_source"] if health else "unknown"

tab_configured, tab_upload = st.tabs(
    [f"Run configured source ({data_source})", "Upload a single .xlsx"]
)

with tab_configured:
    st.write(
        f"This will process every project plan from the configured "
        f"`DATA_SOURCE` (**{data_source}**)."
    )
    if st.button("▶️ Run weekly agent now", type="primary", key="run_configured"):
        with st.spinner("Scoring project plan(s)... this calls the LLM (or fallback) per project."):
            try:
                result = post("/weekly/run", json={})
            except ApiError as exc:
                st.error(str(exc))
            else:
                st.success(f"Processed {result['processed']} project(s), {result['failed']} failure(s).")
                for report in result["reports"]:
                    badge = RAG_BADGE.get(report["final_rag_status"], "⚪")
                    st.markdown(
                        f"{badge} **{report['project_name']}** — "
                        f"{report['final_rag_status']} (composite {report['composite_score']})"
                    )
                for err in result["errors"]:
                    st.error(f"{err['path']}: {err['error']}")
                st.cache_data.clear()

with tab_upload:
    st.write("Upload a single project-plan workbook to score it directly, bypassing `DATA_SOURCE`.")
    uploaded = st.file_uploader("Project plan (.xlsx)", type=["xlsx"])
    if uploaded is not None and st.button("▶️ Run weekly agent on this file", type="primary"):
        with st.spinner(f"Scoring {uploaded.name}..."):
            try:
                report = post(
                    "/weekly/upload",
                    files={"file": (uploaded.name, uploaded.getvalue(),
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                )
            except ApiError as exc:
                st.error(str(exc))
            else:
                badge = RAG_BADGE.get(report["final_rag_status"], "⚪")
                st.success(
                    f"{badge} **{report['project_name']}** — {report['final_rag_status']} "
                    f"(composite {report['composite_score']})"
                )
                st.write(report.get("plain_english_reasoning", ""))
                st.cache_data.clear()
