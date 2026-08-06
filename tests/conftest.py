"""Shared test fixtures for Astarots."""

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def project_root():
    """Absolute path to the project root directory."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def doch_dir(project_root):
    """Path to the documentation directory."""
    return project_root / "doch"
