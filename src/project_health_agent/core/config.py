"""
Central configuration.

All environment-driven values live here as a single validated `Settings`
object (pydantic-settings) so that:
  - a missing/misspelled env var fails fast at startup with a clear error,
    instead of surfacing later as a confusing `KeyError` mid-pipeline;
  - every other module imports `settings` instead of calling `os.getenv`
    directly, so there is exactly one place that knows about env vars;
  - the same code runs unmodified across environments (dev laptop, CI,
    scheduled prod container) purely by swapping the `.env` file / injected
    env vars — see `.env.example` and `docs/GOOGLE_DRIVE_SETUP.md`.

Tunable business constants (scoring weights, band thresholds, override
rules) are kept separate, below, as plain module-level dicts rather than
settings fields. That split is deliberate: they are versioned methodology
values a PMO owner reviews in code review, not deployment-time secrets, and
keeping them out of the LLM-provider-key blast radius means a bad `.env` on
someone's laptop can never silently change how a project gets scored.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = three levels up from this file (src/project_health_agent/core/config.py)
BASE_DIR = Path(__file__).resolve().parents[3]

_ENV_FILE = os.getenv("PHA_ENV_FILE", str(BASE_DIR / ".env"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Runtime environment -------------------------------------------------
    ENVIRONMENT: Literal["development", "staging", "production", "test"] = "development"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["text", "json"] = "text"
    TODAYS_DATE_OVERRIDE: str = ""  # YYYY-MM-DD, for reproducible demo/test runs

    # --- LLM provider selection -----------------------------------------------
    # One of: "groq", "gemini", "none". "none" (or missing keys) triggers the
    # deterministic fallback reasoner, so the pipeline is always runnable
    # end-to-end without an API key.
    LLM_PROVIDER: Literal["groq", "gemini", "none"] = "none"
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"

    # --- Data source: where project-plan workbooks come from -----------------
    # "local": read *.xlsx from LOCAL_DATA_DIR (default: sample_data/) — used
    #          for local dev, demos, and CI, where nothing should reach the
    #          network.
    # "drive": sync every spreadsheet in a Google Drive folder before each
    #          run, so a PM updates the workbook in the shared folder and the
    #          next scheduled run just picks it up — no manual file handling.
    #          See docs/GOOGLE_DRIVE_SETUP.md.
    DATA_SOURCE: Literal["local", "drive"] = "local"
    LOCAL_DATA_DIR: str = str(BASE_DIR / "sample_data")

    # Accepts either a full folder URL
    # (https://drive.google.com/drive/folders/<ID>) or a bare folder ID —
    # the ingestion layer normalizes it (see ingestion/drive_client.py).
    DRIVE_FOLDER_URL: str = ""

    # Path to a service-account JSON key file (mounted secret / local file).
    # Preferred for scheduled/unattended runs since it never expires and
    # needs no human to click through an OAuth consent screen.
    GOOGLE_SERVICE_ACCOUNT_FILE: str = ""

    # Alternative to the file above: the service-account JSON contents,
    # base64-encoded, so CI can inject it as a single secret env var
    # (GitHub Actions secrets can't hold multi-line files cleanly).
    GOOGLE_SERVICE_ACCOUNT_JSON_BASE64: str = ""

    # Local cache dir that synced Drive files land in before ingestion.
    # Kept out of source control (see .gitignore) — it's a cache, not data.
    DRIVE_CACHE_DIR: str = str(BASE_DIR / "var" / "cache" / "drive")

    # Network resilience for the Drive API (see ingestion/drive_client.py).
    DRIVE_MAX_RETRIES: int = 4
    DRIVE_TIMEOUT_SECONDS: int = 30

    # --- Output paths ----------------------------------------------------------
    WEEKLY_OUTPUT_DIR: str = str(BASE_DIR / "var" / "outputs" / "weekly")
    MONTHLY_OUTPUT_DIR: str = str(BASE_DIR / "var" / "outputs" / "monthly")

    @model_validator(mode="after")
    def _validate_data_source(self) -> Settings:
        if self.DATA_SOURCE == "drive" and not self.DRIVE_FOLDER_URL:
            raise ValueError(
                "DATA_SOURCE=drive requires DRIVE_FOLDER_URL to be set "
                "(paste the Google Drive folder link). See "
                "docs/GOOGLE_DRIVE_SETUP.md."
            )
        return self


settings = Settings()

# --- RAG scoring weights (Phase 1 methodology) ------------------------------
SIGNAL_WEIGHTS = {
    "schedule_slippage": 25,
    "progress_vs_plan": 20,
    "blockers": 20,
    "stakeholder_sentiment": 15,
    "critical_path_exposure": 10,
    "data_completeness": 10,
}

BAND_THRESHOLDS = {
    "green_min": 80,   # composite >= 80 -> Green
    "amber_min": 50,   # 50 <= composite < 80 -> Amber, else Red
}

OVERRIDE_RULES = {
    "blocker_age_force_red_days": 21,
    "critical_slip_pct_of_duration_force_red": 0.20,
    "data_completeness_amber_cap": 0.80,  # below this, cap at Amber max
    "disagreement_band_gap_for_review": 1,  # bands apart vs source RAG column
}

# --- Semantic signal-detection thresholds (see scoring/semantic_signals.py) -
SEMANTIC_THRESHOLDS = {
    "blocker_similarity_min": 0.10,     # TF-IDF cosine similarity vs BLOCKER_EXEMPLARS
    "sentiment_similarity_min": 0.10,   # TF-IDF cosine similarity vs NEGATIVE_SENTIMENT_EXEMPLARS
    "theme_bm25_min": 5.0,              # BM25 score vs THEME_EXEMPLARS index
}
