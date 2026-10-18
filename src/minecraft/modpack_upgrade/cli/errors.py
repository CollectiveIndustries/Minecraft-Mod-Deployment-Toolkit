# src/minecraft/modpack_upgrade/cli/errors.py

"""CLI-specific errors with exit codes from spec v1.3 Section 16.2."""

from __future__ import annotations

__all__ = ["UpgradeCliError"]


class UpgradeCliError(Exception):
    """Raised by a CLI command to abort with a specific exit code.

    The top-level ``main`` catches these, prints the message to stderr,
    and returns ``exit_code``. Any uncaught exception becomes exit 1.
    """

    def __init__(self, exit_code: int, message: str) -> None:
        """Store the exit code and message."""
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message
