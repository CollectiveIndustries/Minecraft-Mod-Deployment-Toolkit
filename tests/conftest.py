# tests/conftest.py
"""Shared fixtures and configuration for pytest."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def temp_dir():
    """Create a temporary directory and return its Path."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def mock_config_manager():
    """Mock ConfigManager and its methods."""
    with patch("src.minecraft.common.config.ConfigManager") as MockConfigManager:
        mock_mgr = MagicMock()
        mock_mgr.file.return_value = mock_mgr
        mock_mgr.env.return_value = mock_mgr
        mock_mgr.cli.return_value = mock_mgr
        mock_mgr.load.return_value = {"some_key": "some_value"}
        MockConfigManager.return_value = mock_mgr
        yield mock_mgr


@pytest.fixture
def sample_manifest_data():
    """Returns a sample manifest dictionary containing mod entries with fields id, side, file, and enabled. Used for testing or demonstration purposes."""
    return {
        "mods": [
            {"id": "mod1", "side": "both", "file": "mod1.jar", "enabled": True},
            {"id": "mod2", "side": "client", "file": "mod2.jar", "enabled": True},
            {"id": "mod3", "side": "server", "file": "mod3.jar", "enabled": False},
        ]
    }


@pytest.fixture(autouse=True)
def mock_logging():
    """Patch LoggingCore setup and get_logger globally."""
    with patch("LoggingCore.setup_logging") as mock_setup, patch("LoggingCore.get_logger") as mock_get:
        mock_logger = MagicMock()
        mock_get.return_value = mock_logger
        yield (mock_setup, mock_logger)
