"""
Data-source abstraction.

Every caller (CLI, tests, notebooks) asks this module "what project plans
should I process right now?" and gets back a list of local file paths — it
never needs to know whether those paths came from `sample_data/` or were
just synced down from a Google Drive folder. That indirection is what makes
`DATA_SOURCE=local` -> `DATA_SOURCE=drive` a one-line config change instead
of a code change (see core/config.py, docs/GOOGLE_DRIVE_SETUP.md).
"""
from __future__ import annotations

import glob
from pathlib import Path

from project_health_agent.core.config import settings
from project_health_agent.core.exceptions import DataSourceError
from project_health_agent.core.logging_config import get_logger
from project_health_agent.ingestion import drive_client

logger = get_logger("ingestion.source")


def get_project_plan_paths(explicit_path: str | None = None) -> list[str]:
    """
    Resolve the list of project-plan workbook paths to process this run.

    - `explicit_path` (e.g. a CLI argument) always wins, for one-off runs
      against a specific file, regardless of DATA_SOURCE.
    - Otherwise, resolved from whichever source is configured:
        DATA_SOURCE=local -> every *.xlsx in LOCAL_DATA_DIR
        DATA_SOURCE=drive -> every workbook currently in DRIVE_FOLDER_URL,
                              synced to DRIVE_CACHE_DIR first
    """
    if explicit_path:
        return [explicit_path]

    if settings.DATA_SOURCE == "drive":
        logger.info("Data source: Google Drive folder (%s)", settings.DRIVE_FOLDER_URL)
        drive_paths = drive_client.sync_folder(settings.DRIVE_FOLDER_URL, settings.DRIVE_CACHE_DIR)
        return [str(p) for p in drive_paths]

    logger.info("Data source: local directory (%s)", settings.LOCAL_DATA_DIR)
    local_dir = Path(settings.LOCAL_DATA_DIR)
    if not local_dir.exists():
        raise DataSourceError(f"LOCAL_DATA_DIR does not exist: {local_dir}")
    local_paths: list[str] = sorted(glob.glob(str(local_dir / "*.xlsx")))
    if not local_paths:
        raise DataSourceError(f"No .xlsx files found in {local_dir}")
    return local_paths
