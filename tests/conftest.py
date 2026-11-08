# tests/conftest.py
"""Shared fixtures and configuration for pytest.

Logging contract for tests
==========================

Application modules emit exclusively through :mod:`LoggingCore`. They never
call :mod:`logging` directly. To make those events observable from pytest,
this conftest installs two sinks on a per-test :class:`LoggingCore`:

1. **CLI/stream sink.** :class:`RoutingConsoleSink` splits CLI loggers
   (prefix-matched) to stdout/stderr verbatim and routes everything else to
   stderr with a ``[LEVEL] name: msg`` prefix. Tests that assert on
   ``capsys.readouterr()`` observe this sink's output.

2. **Recording + stdlib bridge.** :class:`PytestLoggingCoreSink` records
   every :class:`LogEvent` in ``sink.events`` and re-emits it through stdlib
   logging under the same logger name. Tests that assert on
   ``caplog.records`` / ``caplog.text`` observe this side.

Both sinks implement ``write_sync``. LoggingCore 0.12.x's
``SyncLogger._emit`` dispatches through ``LoggingCore.emit_sync``, which
skips any sink that only provides ``async def write``; a sink without
``write_sync`` is invisible to every sync log call.

State is manipulated through :mod:`LoggingCore._state`'s public accessors
rather than the underscored ``_logging_core`` / ``_logger_cache``
attributes that prior LoggingCore versions kept on ``sync_logger``.
"""

from __future__ import annotations

import logging
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from LoggingCore import LogEvent, LoggingCore, _state
from LoggingCore.sinks import RoutingConsoleSink

from minecraft.common.cli_logging import CLI_PREFIXES

# ---------------------------------------------------------------------------
# LoggingCore test sinks
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
    process. ``write_sync`` is required so ``emit_sync`` dispatches to this
    sink; ``write`` is the async counterpart for ``emit``.
    """

    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def write_sync(self, event: LogEvent) -> None:
        """Record the event, then re-emit it through stdlib logging."""
        self.events.append(event)
        stdlib_logger = logging.getLogger(event.logger)
        level = _STDLIB_LEVELS.get(event.level.upper(), logging.INFO)
        stdlib_logger.log(level, event.message)

    async def write(self, event: LogEvent) -> None:
        """Record the event, then re-emit it through stdlib logging (async)."""
        self.write_sync(event)


@pytest.fixture(autouse=True)
def logging_core_sink() -> Iterator[PytestLoggingCoreSink]:
    """Install a fresh LoggingCore test stack for every test.

    Replaces the global core with one that has:

      * a :class:`RoutingConsoleSink` for CLI prefixes (writes to
        stdout/stderr, visible to ``capsys``)
      * a :class:`PytestLoggingCoreSink` that records events and bridges
        them to stdlib logging (visible to ``caplog``)

    Both sinks implement ``write_sync`` because LoggingCore 0.12.x's
    ``SyncLogger._emit`` dispatches through ``emit_sync``.
    """
    sink = PytestLoggingCoreSink()
    core = LoggingCore()
    core.add_sink(sink)
    core.add_sink(RoutingConsoleSink(cli_prefixes=CLI_PREFIXES, color=False))
    core.set_level(logging.DEBUG)

    previous_core = _state.get_core()
    previous_cache = dict(_state.get_logger_cache())
    _state.set_core(core)
    _state.get_logger_cache().clear()

    try:
        yield sink
    finally:
        _state.set_core(previous_core)
        _state.get_logger_cache().clear()
        _state.get_logger_cache().update(previous_cache)


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
