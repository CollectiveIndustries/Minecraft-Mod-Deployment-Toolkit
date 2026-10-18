# src/minecraft/common/cli_logging.py

"""CLI-friendly LoggingCore configuration with a synchronous emit patch.

Every message in this project -- user-facing CLI output and diagnostic
logs alike -- flows through LoggingCore. This module configures the
single ``LoggingCore`` instance for the process and installs a sink
that routes events by logger name:

  * Loggers whose name starts with a CLI prefix emit to ``sys.stdout``
    (INFO/DEBUG) or ``sys.stderr`` (WARNING/ERROR/CRITICAL), message
    verbatim. This is the CLI output channel.
  * All other loggers emit to ``sys.stderr``, formatted with
    ``[LEVEL] logger.name:``.

Synchronous emit patch
----------------------

LoggingCore's ``SyncLogger`` uses ``loop.create_task`` when an event
loop is running (as it always is under ``pytest-asyncio`` auto mode).
That defers output past the next statement, so ``capsys.readouterr()``
inside a test sees nothing. ``configure()`` patches
``SyncLogger._emit`` so CLI and diagnostic loggers write directly to
the appropriate stream on the calling thread. This also makes
``capsys`` and ``caplog`` fixtures work as test suites expect.

The patch is idempotent (guarded by ``_patched``).

Stdlib forwarding
-----------------

Every emit also forwards the message to the stdlib logger of the same
name at the matching level, so ``pytest``'s ``caplog`` fixture can
capture events. A ``NullHandler`` on the ``minecraft`` logger prevents
Python's ``lastResort`` handler from duplicating output to stderr in
production (where ``caplog`` is not installed).

Rationale
---------

Before this module, ``print()`` and ``sys.stderr.write()`` were used for
CLI output. That bypassed LoggingCore, so sinks could not be swapped
(color, file, structured JSONL) and the ruff T201/TID251 bans had to be
suppressed per-file. Routing everything through one core with a single
stream-aware sink closes those gaps: the sink is the only place any
stream is written.

Usage
-----

Entry points call ``configure()`` once at startup:

    from minecraft.common.cli_logging import configure
    configure(debug=args.debug)

Commands obtain CLI-namespaced loggers (via ``get_logger`` from
``LoggingCore``; the patch makes them synchronous automatically):

    from LoggingCore import get_logger
    _log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.verify")

Non-CLI loggers keep using ``__name__``:

    from LoggingCore import get_logger
    _log = get_logger(__name__)

Idempotency
-----------

``configure()`` is safe to call more than once. If the current core
already has a ``RoutingSink``, only the minimum level is adjusted (so
a ``--debug`` restart is honoured).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import LoggingCore.sync_logger as _sync_module
from LoggingCore import LoggingCore
from LoggingCore.LogManager import LogEvent, LogSink

__all__ = [
    "CLI_PREFIXES",
    "RoutingSink",
    "configure",
    "reset",
]

# Loggers whose name starts with one of these prefixes emit CLI output.
CLI_PREFIXES: tuple[str, ...] = (
    "minecraft.modpack_upgrade.cli.",
    "minecraft.deploy_pack.cli.",
)

_LEVEL_COLORS: dict[str, str] = {
    "DEBUG": "\x1b[36m",
    "INFO": "\x1b[32m",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[31m",
    "CRITICAL": "\x1b[35m",
}

_ERROR_LEVELS: frozenset[str] = frozenset({"WARNING", "ERROR", "CRITICAL"})

_patched: bool = False


def _is_cli_logger(name: str) -> bool:
    """Return True if ``name`` is in a CLI namespace."""
    return any(name.startswith(prefix) for prefix in CLI_PREFIXES)


def _format_diagnostic(level: str, name: str, message: str, fields: dict | None, exception: str | None) -> str:
    """Render a diagnostic log line: ``[LEVEL] logger: message [fields]``."""
    reset = "\x1b[0m"
    tint = _LEVEL_COLORS.get(level.upper(), "")
    line = f"{tint}[{level.upper()}]{reset} {name}: {message}"
    if fields:
        line += f" {fields}"
    if exception:
        line += f"\n{exception}"
    return line


class RoutingSink(LogSink):
    """Route log events by logger name and level.

    Retained for backwards compatibility with any code that adds sinks
    directly. The synchronous emit patch bypasses this sink; if future
    work needs the sink path (e.g., file sinks), remove the patch and
    flush explicitly at end-of-command.
    """

    def __init__(self, color: bool = True) -> None:
        self.color = color

    async def write(self, event: LogEvent) -> None:
        """Write the event to the correct stream with the correct format."""
        if _is_cli_logger(event.logger):
            is_error = event.level.upper() in _ERROR_LEVELS
            stream = sys.stderr if is_error else sys.stdout
            stream.write(event.message + "\n")
        else:
            stream = sys.stderr
            stream.write(_format_diagnostic(event.level, event.logger, event.message, event.fields, event.exception) + "\n")
        stream.flush()


def _has_routing_sink(core: LoggingCore) -> bool:
    """Return True if ``core`` already has a RoutingSink registered."""
    return any(isinstance(sink, RoutingSink) for sink in core._sinks)


def _patch_sync_emit() -> None:
    """Make ``SyncLogger._emit`` write synchronously.

    LoggingCore's ``SyncLogger._emit`` uses ``loop.create_task`` when a
    loop is running, deferring output. This is correct for a running
    daemon (Discord bot, HTTP server) but wrong for CLI tools and for
    pytest tests using ``capsys``. The patch writes directly on the
    caller's thread and forwards to stdlib logging for ``caplog``.

    Idempotent: guarded by ``_patched``.
    """
    global _patched
    if _patched:
        return
    _patched = True

    def patched_emit(self: Any, level: str, message: str, fields: dict | None = None, exception: str | None = None) -> None:
        is_cli = _is_cli_logger(self.name)
        is_error = level.upper() in _ERROR_LEVELS

        if is_cli:
            stream = sys.stderr if is_error else sys.stdout
            line = message
        else:
            stream = sys.stderr
            line = _format_diagnostic(level, self.name, message, fields, exception)

        stream.write(line + "\n")
        stream.flush()

        # Forward to stdlib logging so pytest's caplog fixture captures
        # events. NullHandler on the "minecraft" logger prevents the
        # stdlib lastResort handler from duplicating output in production.
        log_level = getattr(logging, level.upper(), logging.INFO)
        logging.getLogger(self.name).log(log_level, message)

    _sync_module.SyncLogger._emit = patched_emit


def configure(debug: bool = False) -> None:
    """Install the global LoggingCore and apply the synchronous emit patch.

    Idempotent. Also sets the stdlib root logger's level so tests can
    assert on ``logging.getLogger().level``.
    """
    level = 10 if debug else 20
    logging.getLogger().setLevel(level)
    # Prevent stdlib lastResort from duplicating our synchronous writes.
    logging.getLogger("minecraft").addHandler(logging.NullHandler())

    existing = _sync_module._logging_core
    if existing is not None and _has_routing_sink(existing):
        existing.set_level(level)
        _patch_sync_emit()
        return

    core = LoggingCore()
    core.add_sink(RoutingSink(color=True))
    core.set_level(level)
    _sync_module._logging_core = core
    _sync_module._logger_cache.clear()
    _patch_sync_emit()


def reset() -> None:
    """Reset the global LoggingCore to its unconfigured state.

    For tests only.
    """
    _sync_module._logging_core = None
    _sync_module._logger_cache.clear()
