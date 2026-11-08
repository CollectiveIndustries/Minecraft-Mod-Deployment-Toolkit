# src/minecraft/common/cli_logging.py

"""Project-specific LoggingCore configuration.

Wires the project's CLI prefixes into LoggingCore's routing sink and
exposes a ``reset()`` that delegates to ``LoggingCore.reset``. Tests that
need a clean core between cases call ``cli_logging.reset()``, not
``LoggingCore._state`` directly.

The stdlib ``logging`` root level is kept in sync with the LoggingCore
minimum. This is not because application code emits through stdlib - it
doesn't, it emits through LoggingCore. It's because pytest's ``caplog``
fixture (and any third-party handler that attaches to the root logger)
needs the root logger's level to be permissive enough to let the bridge
in ``tests/conftest.py`` propagate warnings and errors into ``caplog``.
Setting it here is cheap and keeps the test bridge transparent.
"""

from __future__ import annotations

import logging
from typing import Any

from LoggingCore import get_logger as _core_get_logger
from LoggingCore import reset as _core_reset
from LoggingCore import setup_logging as _core_setup_logging

__all__ = ["CLI_PREFIXES", "configure", "get_logger", "reset"]

CLI_PREFIXES: tuple[str, ...] = (
    "minecraft.modpack_upgrade.cli.",
    "minecraft.deploy_pack.cli.",
)


def configure(debug: bool = False) -> None:
    """Install the routing sink and set the diagnostic channel's level.

    ``debug=False`` sets the minimum to INFO, so DEBUG events are dropped
    at the source. ``debug=True`` lowers it to DEBUG. Safe to call more
    than once; a reconfigure updates the level and clears the logger
    cache so loggers captured before the reconfigure observe the change.

    Also sets the stdlib root logger's level to match. See the module
    docstring for why.
    """
    level_name = "DEBUG" if debug else "INFO"
    _core_setup_logging(
        {
            "level": level_name,
            "color": True,
            "handlers": [{"type": "console"}],
            "cli_prefixes": list(CLI_PREFIXES),
        }
    )
    logging.getLogger().setLevel(logging.DEBUG if debug else logging.INFO)


def get_logger(name: str) -> Any:
    """Return a LoggingCore SyncLogger labelled with ``name``."""
    return _core_get_logger(name)


def reset() -> None:
    """Reset the global LoggingCore to unconfigured state (tests only)."""
    _core_reset()
