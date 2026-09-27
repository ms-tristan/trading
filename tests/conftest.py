"""Shared fixtures for the ``trading_platform`` test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def repo_root() -> Path:
    """Absolute path of the checkout under test."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    """A temporary directory that stands in for ``data/realtime``."""
    directory = tmp_path / "realtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory
