from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DATA_DIR = REPO_ROOT / "sample_data"


@pytest.fixture
def project_a_path() -> str:
    return str(SAMPLE_DATA_DIR / "Project_A.xlsx")


@pytest.fixture
def project_b_path() -> str:
    return str(SAMPLE_DATA_DIR / "Project_B.xlsx")
