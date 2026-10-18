# src/minecraft/modpack_upgrade/cli/commands/stubs.py

"""Placeholder implementations for not-yet-implemented commands."""

from __future__ import annotations

from LoggingCore import get_logger

__all__ = ["not_implemented"]

_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.stubs")


def not_implemented(name: str, hint: str | None = None) -> int:
    """Emit a "not implemented" message and return exit code 1."""
    _log_cli.error(f"error: {name!r} is not yet implemented")
    if hint:
        _log_cli.error(f"  hint: {hint}")
    return 1
