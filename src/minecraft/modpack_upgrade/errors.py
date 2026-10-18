# src/minecraft/modpack_upgrade/errors.py

"""Exception hierarchy for the upgrade system (spec Section 14.2)."""

from __future__ import annotations


class UpgradeError(Exception):
    """Base exception for all upgrade-related errors."""

    exit_code: int = 1


class UpgradeUsageError(UpgradeError):
    """Raised when the upgrade command is used incorrectly."""

    exit_code = 2


class UpgradeConfigError(UpgradeError):
    """Raised when the upgrade configuration is invalid or missing."""

    exit_code = 3


class ProviderError(UpgradeError):
    """A provider API call or artifact download failed (spec Section 16.2 exit 1).

    Covers: network failure, HTTP error, malformed response, declared-hash
    mismatch on a downloaded artifact, or an oversized download. Never
    used for a "no compatible version" outcome - that is a resolution
    result, not an error.
    """

    exit_code = 1


class ResolutionError(UpgradeError):
    """Raised when dependency or version resolution fails."""

    exit_code = 4


class ValidationGateError(UpgradeError):
    """Raised when a validation gate fails during upgrade."""

    exit_code = 5


class ApprovalBlockedError(UpgradeError):
    """Raised when an upgrade is blocked due to missing approval."""

    exit_code = 6


class PublicationError(UpgradeError):
    """Raised when publishing an upgrade fails."""

    exit_code = 7


class SchemaError(UpgradeConfigError):
    """A TOML document does not conform to its schema."""


class SlipError(SchemaError):
    """A packing slip is invalid."""


class HashMismatchError(SlipError):
    """A computed hash does not match a recorded hash."""
