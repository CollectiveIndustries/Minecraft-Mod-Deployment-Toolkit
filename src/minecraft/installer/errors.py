# src/minecraft/installer/errors.py

"""Exception types for the installer."""

from __future__ import annotations

__all__ = [
    "ComposeError",
    "ConfigError",
    "InstallerError",
    "SecretsError",
]


class InstallerError(Exception):
    """Base class for all installer failures."""

    exit_code: int = 1


class SecretsError(InstallerError):
    """A secret could not be generated, read, or validated."""


class ComposeError(InstallerError):
    """A docker compose operation failed."""


class ConfigError(InstallerError):
    """Installer configuration could not be read."""
