"""Trigger Phase 3: aggregate one month's weekly outputs and render the executive deck."""
from __future__ import annotations

import streamlit as st

from api_client import ApiError, get, post, render_api_status_sidebar

st.set_page_config(page_title="Monthly Synthesis", page_icon="\U0001F4C8", layout="wide")
render_api_status_sidebar()

st.title("Monthly Portfolio Synthesis")
st.caption(
    "Aggregates one calendar month's weekly reports into a portfolio view, has the "
    "LLM (or deterministic fallback) author the executive narrative, and "
    "renders the .pptx deck. Requires at least one weekly run in that month."
)

try:
    months = get("/monthly/months")
except ApiError as exc:
    st.error(str(exc))
    st.stop()

with_weekly_data = months.get("with_weekly_data", [])
generated = set(months.get("generated", []))

if not with_weekly_data:
    st.info("No weekly reports yet — head to **Run Agent** to generate the first one.")
    st.stop()

# Union of "months we could report on" and "months already reported on",
# newest first, so a previously-generated month is still pickable even if
# no new weekly run has landed for it recently.
all_months = sorted(set(with_weekly_data) | generated, reverse=True)


def _label(m: str) -> str:
    return f"{m}  {'✅ generated' if m in generated else '— not yet generated'}"


selected_month = st.selectbox("Month", all_months, format_func=_label)

if st.button(f"▶️ Run monthly synthesis for {selected_month}", type="primary"):
    with st.spinner(f"Aggregating {selected_month}, synthesizing narrative, and rendering deck..."):
        try:
            result = post("/monthly/run", json={"month": selected_month})
        except ApiError as exc:
            st.error(str(exc))
        else:
            st.success(f"Monthly synthesis for {selected_month} complete.")
            st.cache_data.clear()
            st.session_state["monthly_result"] = result

st.divider()

# Prefer a freshly-run result for this exact month in this session;
# otherwise fall back to whatever's already on disk for the selected month
# (CLI or an earlier session).
fresh = st.session_state.get("monthly_result")
if fresh and fresh.get("month") == selected_month:
    package = fresh.get("portfolio_package")
    slide_plan = fresh.get("slide_plan")
else:
    package = None
    slide_plan = None

if package is None:
    try:
        package = get("/monthly/package", params={"month": selected_month})
    except ApiError:
        package = None

if slide_plan is None:
    try:
        slide_plan = get("/monthly/slide_plan", params={"month": selected_month})
    except ApiError:
        slide_plan = None

if package is None:
    st.info(f"No portfolio package for {selected_month} yet. Run the synthesis above to generate one.")
    st.stop()

st.subheader(f"Portfolio package — {selected_month}")
col1, col2 = st.columns(2)
col1.metric("Projects in this package", package["generated_from_projects"])
col2.write("**Band mix:** " + ", ".join(f"{k}: {v}" for k, v in package.get("band_mix", {}).items()))

if package.get("recurring_themes"):
    st.write("**Recurring themes:** " + ", ".join(package["recurring_themes"]))

if slide_plan:
    st.subheader(f"Slide plan ({len(slide_plan.get('slides', []))} slides, generated_by: {slide_plan.get('generated_by')})")
    for i, slide in enumerate(slide_plan.get("slides", []), start=1):
        with st.expander(f"Slide {i}: {slide.get('title', slide.get('layout', ''))}"):
            st.json(slide)

st.divider()
try:
    deck_bytes = get("/monthly/deck", params={"month": selected_month})
    st.download_button(
        f"⬇️ Download Executive_Portfolio_Review_{selected_month}.pptx",
        data=deck_bytes,
        file_name=f"Executive_Portfolio_Review_{selected_month}.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
except ApiError:
    st.caption("No deck available yet for this month.")
