# src/minecraft/deploy_pack/logging_setup.py

"""Per-module LoggingCore loggers for deploy_pack.

Every deploy_pack module obtains its logger via :func:`get_logger`. The
label recorded on each event is the module's ``__name__``, so an event
from ``scope_server`` shows up as
``minecraft.deploy_pack.scope_server``.

Log records are routed to stderr by a ConsoleSink variant so that
structured CLI output (recovery blocks, per-container status, failure
summaries) can keep using stdout without interleaving.

Configuration runs exactly once, from ``main``, before any other module
emits. LoggingCore's stock ``setup_logging`` installs a stdout sink and
is therefore bypassed here.

Cache invalidation
------------------

``get_logger`` caches per-name logger instances in
``LoggingCore.sync_logger._logger_cache``. When :func:`configure`
replaces ``_logging_core``, the cache must be cleared so subsequent
lookups resolve against the new core. Without this, module-level
loggers captured at import time keep speaking to the old core and their
level filter reflects the old core's level. ``--debug`` would set level
10 on the new core and every module-level ``logger.debug(...)`` call
would be silently dropped.
"""

from __future__ import annotations

import sys
from typing import Any

import LoggingCore.sync_logger as _sync_module
from LoggingCore import get_logger as _get_core_logger
from LoggingCore.LogManager import LogEvent, LoggingCore
from LoggingCore.sinks.console import ConsoleSink

_configured = False
_LEVEL_COLORS = {"DEBUG": "\x1b[36m", "INFO": "\x1b[32m", "WARNING": "\x1b[33m", "ERROR": "\x1b[31m", "CRITICAL": "\x1b[35m"}


class _StderrConsoleSink(ConsoleSink):
    """ConsoleSink that writes to stderr. Same format, different stream."""

    async def write(self, event: LogEvent) -> None:
        """Write a log event to stderr, optionally colorized."""
        reset = "\x1b[0m" if self.color else ""
        color = _LEVEL_COLORS.get(event.level.upper(), "") if self.color else ""
        line = f"{color}[{event.level.upper()}]{reset} {event.logger}: {event.message}"
        if event.fields:
            line += f" {event.fields}"
        if event.exception:
            line += f"\n{event.exception}"
        sys.stderr.write(line + "\n")
        sys.stderr.flush()


def configure(debug: bool = False) -> None:
    """Install the global LoggingCore with a stderr sink. Idempotent."""
    global _configured
    if _configured:
        return
    core = LoggingCore()
    core.add_sink(_StderrConsoleSink(color=True))
    core.set_level(10 if debug else 20)
    _sync_module._logging_core = core
    _sync_module._logger_cache.clear()
    _configured = True


def get_logger(name: str) -> Any:
    """Return a LoggingCore SyncLogger labelled with ``name``."""
    return _get_core_logger(name)
