"""
CLI entrypoint for Phase 2: run the weekly Project Health Reporting Agent.

Usage:
    python -m project_health_agent.cli.weekly                  # process every plan from the configured data source
    python -m project_health_agent.cli.weekly --path plan.xlsx # process a single project plan, ignoring DATA_SOURCE
    project-health-weekly                                       # same, via the installed console script (see pyproject.toml)

Data source (local folder vs. Google Drive folder) is controlled entirely by
`DATA_SOURCE` in the environment — see core/config.py and
docs/GOOGLE_DRIVE_SETUP.md. This module doesn't know or care which one is
active; it only calls `get_project_plan_paths()`.

Writes, per project, per run:
    <WEEKLY_OUTPUT_DIR>/<project_slug>/<week_ending>.json
    <WEEKLY_OUTPUT_DIR>/<project_slug>/<week_ending>.docx
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import typer

from project_health_agent.core.config import settings
from project_health_agent.core.exceptions import ProjectHealthAgentError
from project_health_agent.core.logging_config import configure_logging, get_logger
from project_health_agent.ingestion.source import get_project_plan_paths
from project_health_agent.reasoning.graph import run_weekly_agent
from project_health_agent.reporting.docx_builder import build_weekly_docx

app = typer.Typer(add_completion=False, help="Run the weekly project health agent.")
logger = get_logger("cli.weekly")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def process_one(path: str) -> dict:
    logger.info("Running weekly agent on: %s", path)
    report = run_weekly_agent(path)

    slug = _slugify(report["project_name"])
    out_dir = Path(settings.WEEKLY_OUTPUT_DIR) / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    week_ending = datetime.now(UTC).strftime("%Y-%m-%d")
    json_path = out_dir / f"{week_ending}.json"
    docx_path = out_dir / f"{week_ending}.docx"

    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    build_weekly_docx(report, str(docx_path))

    logger.info(
        "%s -> %s (composite %s) | wrote %s and %s",
        report["project_name"], report["final_rag_status"], report["composite_score"],
        json_path, docx_path,
    )
    return report


@app.command()
def main(
    path: str | None = typer.Option(
        None, "--path", "-p",
        help="Process a single .xlsx file, bypassing DATA_SOURCE.",
    ),
) -> None:
    configure_logging()
    try:
        paths = get_project_plan_paths(explicit_path=path)
    except ProjectHealthAgentError as exc:
        logger.error("Could not resolve project plans to process: %s", exc)
        raise typer.Exit(code=1) from exc

    failures = 0
    for p in paths:
        try:
            process_one(p)
        except ProjectHealthAgentError as exc:
            failures += 1
            logger.error("Failed to process %s: %s", p, exc)

    if failures:
        logger.error("%d of %d project plan(s) failed.", failures, len(paths))
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
