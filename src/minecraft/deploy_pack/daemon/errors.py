# src/minecraft/deploy_pack/daemon/errors.py

"""Minecraft-specific daemon errors.

Each subclass carries a numeric code (spec v1.3 Section 16.3) and an HTTP status
for the plugin to use when shaping responses. Codes 20-26 are reserved
for Minecraft; the plugin translates these to HTTP responses.
"""

from __future__ import annotations

__all__ = [
    "ActivationFailedError",
    "DeployCommandFailedError",
    "DeploymentLockHeldError",
    "IdempotencyConflictError",
    "MinecraftDaemonError",
    "NotImplementedError",
    "PostDeployVerifyFailedError",
    "RecoveryFailedError",
    "RegistryChainFailedError",
    "VerificationBlockedError",
]


class MinecraftDaemonError(Exception):
    """Base Minecraft daemon error. HTTP 500, code 1."""

    http_status: int = 500
    code: int = 1

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class VerificationBlockedError(MinecraftDaemonError):
    """Release verification blocked (spec Section 16.3 code 20)."""

    http_status = 409
    code = 20


class ActivationFailedError(MinecraftDaemonError):
    """Activation failed (spec Section 16.3 code 21)."""

    http_status = 500
    code = 21


class DeployCommandFailedError(MinecraftDaemonError):
    """Deploy command failed (spec Section 16.3 code 22)."""

    http_status = 500
    code = 22


class PostDeployVerifyFailedError(MinecraftDaemonError):
    """Post-deploy verification failed (spec Section 16.3 code 23)."""

    http_status = 500
    code = 23


class RecoveryFailedError(MinecraftDaemonError):
    """Recovery/rollback failed (spec Section 16.3 code 24)."""

    http_status = 500
    code = 24


class DeploymentLockHeldError(MinecraftDaemonError):
    """Concurrent deployment lock held (spec Section 16.3 code 25)."""

    http_status = 409
    code = 25


class RegistryChainFailedError(MinecraftDaemonError):
    """Registry chain verification failed (spec Section 16.3 code 26)."""

    http_status = 409
    code = 26


class IdempotencyConflictError(MinecraftDaemonError):
    """Idempotency-Key reused with a different request body."""

    http_status = 409
    code = 27


class NotImplementedError(MinecraftDaemonError):
    """Endpoint present but deferred to a later slice."""

    http_status = 501
    code = 30
