# tests/conftest.py
"""Shared fixtures and configuration for pytest.

Logging contract for tests
==========================

Application modules emit exclusively through :mod:`LoggingCore`. They never
call :mod:`logging` directly. To make those events observable from pytest,
this conftest installs a :class:`PytestLoggingCoreSink` on a per-test
:class:`LoggingCore` instance. The sink has two jobs:

1. **Native capture.** ``sink.events`` records every :class:`LogEvent` in
   emission order. Tests that want structured access use the
   ``log_events`` fixture.

2. **``caplog`` bridge.** Each event is re-emitted through stdlib logging
   under the same logger name at the matching level. pytest's ``caplog``
   fixture observes stdlib records, so ``caplog.records`` and
   ``caplog.text`` keep working for tests written against them. The bridge
   is test-only; it does not exist in the application.

Each application module chooses its own logger name (usually its
``__name__``); the fixture does not constrain the name.
"""

from __future__ import annotations

import logging
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from LoggingCore import LogEvent, LoggingCore
from LoggingCore import sync_logger as logging_core_sync

# ---------------------------------------------------------------------------
# LoggingCore test sink
# ---------------------------------------------------------------------------

_STDLIB_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class PytestLoggingCoreSink:
    """LoggingCore sink that records events and bridges them to stdlib logging.

    Application code never sees this class; it exists only inside the pytest
    process. See the module docstring for rationale.
    """

    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    async def write(self, event: LogEvent) -> None:
        """Record the event, then re-emit it through stdlib logging."""
        self.events.append(event)
        stdlib_logger = logging.getLogger(event.logger)
        level = _STDLIB_LEVELS.get(event.level.upper(), logging.INFO)
        stdlib_logger.log(level, event.message)


@pytest.fixture(autouse=True)
def logging_core_sink() -> Iterator[PytestLoggingCoreSink]:
    """Install a fresh LoggingCore test sink for every test.

    The global LoggingCore is swapped out so each test sees a clean sink and
    a clean event list. The previous core (and its logger cache) is restored
    on teardown.
    """
    sink = PytestLoggingCoreSink()
    core = LoggingCore()
    core.add_sink(sink)
    core.set_level(logging.DEBUG)

    previous_core = logging_core_sync._logging_core
    previous_cache = dict(logging_core_sync._logger_cache)
    logging_core_sync._logging_core = core
    logging_core_sync._logger_cache.clear()

    try:
        yield sink
    finally:
        logging_core_sync._logging_core = previous_core
        logging_core_sync._logger_cache.clear()
        logging_core_sync._logger_cache.update(previous_cache)


@pytest.fixture
def log_events(logging_core_sink: PytestLoggingCoreSink) -> list[LogEvent]:
    """Events captured through LoggingCore during the current test."""
    return logging_core_sink.events


# ---------------------------------------------------------------------------
# Filesystem / network fixtures
# ---------------------------------------------------------------------------


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
    """Sample manifest for tests."""
    return {
        "mods": [
            {"id": "mod1", "side": "both", "file": "mod1.jar", "enabled": True},
            {"id": "mod2", "side": "client", "file": "mod2.jar", "enabled": True},
            {"id": "mod3", "side": "server", "file": "mod3.jar", "enabled": False},
        ]
    }


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Block every outbound socket connect for the duration of the test."""

    def guard_connect(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Outbound network access is forbidden in tests. Mock the external service instead.")

    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_connect)
    monkeypatch.setattr(socket, "create_connection", guard_connect)
    yield


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every test with CWD set to its own ``tmp_path``."""
    monkeypatch.chdir(tmp_path)
    yield
