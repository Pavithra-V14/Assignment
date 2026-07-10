"""
Thin wrapper around the FastAPI backend (`project_health_agent.api.main`).

Every Streamlit page imports this instead of calling `requests` directly,
so there's exactly one place that knows the base URL, timeout, and error
shape — consistent with the backend's own "one seam per concern" style.
"""
from __future__ import annotations

import os
from typing import Any

import requests
import streamlit as st

API_BASE_URL = os.environ.get("PHA_API_BASE_URL", "http://localhost:8000")
DEFAULT_TIMEOUT = 60


class ApiError(Exception):
    """Raised when the backend returns a non-2xx response or is unreachable."""


def _handle(resp: requests.Response) -> Any:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        if isinstance(detail, dict):
            detail = detail.get("detail", str(detail))
        raise ApiError(str(detail))
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    return resp.content


def get(path: str, **kwargs: Any) -> Any:
    try:
        resp = requests.get(f"{API_BASE_URL}{path}", timeout=DEFAULT_TIMEOUT, **kwargs)
    except requests.exceptions.ConnectionError as exc:
        raise ApiError(
            f"Could not reach the API at {API_BASE_URL}. Is it running? "
            f"(`uvicorn project_health_agent.api.main:app --reload`)"
        ) from exc
    return _handle(resp)


def post(path: str, **kwargs: Any) -> Any:
    try:
        resp = requests.post(f"{API_BASE_URL}{path}", timeout=DEFAULT_TIMEOUT, **kwargs)
    except requests.exceptions.ConnectionError as exc:
        raise ApiError(
            f"Could not reach the API at {API_BASE_URL}. Is it running? "
            f"(`uvicorn project_health_agent.api.main:app --reload`)"
        ) from exc
    return _handle(resp)


@st.cache_data(ttl=15, show_spinner=False)
def cached_health() -> dict | None:
    try:
        return get("/health")
    except ApiError:
        return None


@st.cache_data(ttl=5, show_spinner=False)
def cached_projects() -> list[dict]:
    return get("/projects")


@st.cache_data(ttl=5, show_spinner=False)
def cached_weekly_reports() -> list[dict]:
    return get("/weekly/reports")


def render_api_status_sidebar() -> None:
    """Small status widget every page can call to show backend connectivity."""
    health = cached_health()
    with st.sidebar:
        st.markdown("### Backend status")
        if health is None:
            st.error(f"API unreachable at\n`{API_BASE_URL}`")
        else:
            st.success("API connected")
            st.caption(
                f"env: **{health['environment']}**  \n"
                f"data source: **{health['data_source']}**  \n"
                f"LLM provider: **{health['llm_provider']}**"
            )
        st.caption(f"Base URL: `{API_BASE_URL}`")
