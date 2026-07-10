"""
CLI entrypoint for Phase 3: monthly portfolio synthesis + executive deck.

Usage:
    python -m project_health_agent.cli.monthly                # current calendar month
    python -m project_health_agent.cli.monthly --month 2026-06 # a specific month
    project-health-monthly --month 2026-06   # via the installed console script

Requires at least one weekly run (cli/weekly.py) to have already produced
output under <WEEKLY_OUTPUT_DIR>/ *within that month*. Each run is scoped
to a single calendar month — it never silently pulls in weekly reports
from other months — and writes its outputs to a per-month subfolder so
re-running a later month never clobbers an earlier one:
    <MONTHLY_OUTPUT_DIR>/<YYYY-MM>/portfolio_package.json   (aggregated trend/risk data)
    <MONTHLY_OUTPUT_DIR>/<YYYY-MM>/slide_plan.json          (LLM-authored slide content)
    <MONTHLY_OUTPUT_DIR>/<YYYY-MM>/Executive_Portfolio_Review.pptx
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import typer

from project_health_agent.aggregation.aggregator import build_portfolio_package
from project_health_agent.aggregation.synthesis_agent import synthesize_portfolio
from project_health_agent.core.config import settings
from project_health_agent.core.exceptions import ProjectHealthAgentError
from project_health_agent.core.logging_config import configure_logging, get_logger
from project_health_agent.reporting.deck_builder import build_deck

app = typer.Typer(add_completion=False, help="Run the monthly portfolio synthesis agent.")
logger = get_logger("cli.monthly")

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _current_month() -> str:
    """Current calendar month as "YYYY-MM", honoring TODAYS_DATE_OVERRIDE
    (see core/config.py) so demo/test runs stay reproducible instead of
    silently depending on the machine's clock."""
    if settings.TODAYS_DATE_OVERRIDE:
        return settings.TODAYS_DATE_OVERRIDE[:7]
    return datetime.now(UTC).strftime("%Y-%m")


def run_monthly(month: str | None = None) -> dict:
    """
    Core Phase 3 logic, factored out of the Typer command so it can be
    called identically from the CLI and from the FastAPI layer
    (`api/main.py`) without either one re-implementing it.

    `month`: "YYYY-MM" to scope the report to that single calendar month.
    Defaults to the current month. This is what makes the "monthly"
    report actually monthly instead of aggregating every weekly run ever
    produced, across every month, in one pass.

    Returns the portfolio package, slide plan, and the deck path — the
    same three artifacts written to disk, under
    <MONTHLY_OUTPUT_DIR>/<month>/.
    """
    if month is not None and not _MONTH_RE.match(month):
        raise ProjectHealthAgentError(f"--month must be in YYYY-MM format, got {month!r}.")
    month = month or _current_month()

    out_dir = Path(settings.MONTHLY_OUTPUT_DIR) / month
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Aggregating weekly outputs for %s across all projects...", month)
    package = build_portfolio_package(year_month=month)
    package_path = out_dir / "portfolio_package.json"
    package_path.write_text(json.dumps(package, indent=2, default=str))
    logger.info(
        "%s: %d project(s), band mix: %s",
        month, package["generated_from_projects"], package["band_mix"],
    )

    logger.info("Synthesizing executive narrative and slide content...")
    slide_plan = synthesize_portfolio(package)
    plan_path = out_dir / "slide_plan.json"
    plan_path.write_text(json.dumps(slide_plan, indent=2, default=str))
    logger.info(
        "%d slides planned (generated_by: %s)",
        len(slide_plan.get("slides", [])), slide_plan.get("generated_by"),
    )

    logger.info("Rendering executive deck...")
    deck_path = out_dir / "Executive_Portfolio_Review.pptx"
    build_deck(slide_plan, package, str(deck_path))
    logger.info("Wrote %s", deck_path)

    return {
        "month": month,
        "portfolio_package": package,
        "slide_plan": slide_plan,
        "deck_path": str(deck_path),
    }


@app.command()
def main(
    month: str | None = typer.Option(
        None, "--month", "-m",
        help="Calendar month to report on, as YYYY-MM. Defaults to the current month.",
    ),
) -> None:
    configure_logging()
    try:
        run_monthly(month=month)
    except ProjectHealthAgentError as exc:
        logger.error("Monthly synthesis failed: %s", exc)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
