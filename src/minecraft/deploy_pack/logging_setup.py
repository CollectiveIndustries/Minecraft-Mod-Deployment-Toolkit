# src/minecraft/deploy_pack/logging_setup.py

"""Shim over minecraft.common.cli_logging for deploy_pack modules.

Historically this module installed its own stderr-only ``ConsoleSink``.
As of the LoggingCore unification, it delegates to
:func:`minecraft.common.cli_logging.configure`, which installs a
``RoutingSink`` that routes events by logger name:

  * ``minecraft.deploy_pack.cli.*`` loggers -> stdout (INFO/DEBUG) or
    stderr (WARNING+), message verbatim. This is the CLI output channel.
  * All other loggers (``minecraft.deploy_pack.hooks``, etc.)
    -> stderr, formatted with ``[LEVEL] logger.name:``. Diagnostics.

Module-level ``get_logger`` is preserved as a passthrough so
``from .logging_setup import get_logger; _log = get_logger(__name__)``
continues to work unchanged in every module that already imports it.
"""

from __future__ import annotations

from typing import Any

from LoggingCore import get_logger as _core_get_logger

from minecraft.common.cli_logging import configure as _configure_routing

__all__ = ["configure", "get_logger"]


def configure(debug: bool = False) -> None:
    """Install the routing LoggingCore.

    Idempotent; delegates to
    :func:`minecraft.common.cli_logging.configure`. Call once from the
    entry point, before any logger is used.
    """
    _configure_routing(debug=debug)


def get_logger(name: str) -> Any:
    """Return a LoggingCore SyncLogger labelled with ``name``."""
    return _core_get_logger(name)
