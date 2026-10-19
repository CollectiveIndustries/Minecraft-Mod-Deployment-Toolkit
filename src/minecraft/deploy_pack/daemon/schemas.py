# src/minecraft/deploy_pack/daemon/schemas.py

"""Request and response schemas for the HTTP admin plugin.

The request body is flat - the caller posts ``{"release_id": "v1.0.2"}``
directly rather than nesting under a ``subject`` key. Field validation
rejects path-traversal-shaped inputs; malformed requests get FastAPI's
default 422.

Slice 13 addition: :class:`DeployResponse` for the successful
``/deploy`` path. Errors continue to use :class:`ErrorResponse`.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = [
    "DeployRequest",
    "DeployResponse",
    "ErrorResponse",
    "PublishRequest",
    "PublishResponse",
    "StatusResponse",
    "VerifyRequest",
    "VerifyResponse",
]

_RELEASE_ID_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.\-]+)?$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KNOWN_POINTERS = frozenset({"stable"})


class VerifyRequest(BaseModel):
    """Body for POST /verify and POST /publish."""

    release_id: str | None = None
    release_pointer: str | None = None
    expected_commit_sha: str | None = None
    expected_slip_sha256: str | None = None

    @field_validator("release_id")
    @classmethod
    def _validate_release_id(cls, value: str | None) -> str | None:
        """Reject release IDs that do not match the version pattern."""
        if value is not None and _RELEASE_ID_RE.fullmatch(value) is None:
            raise ValueError(f"invalid release_id {value!r}: must match {_RELEASE_ID_RE.pattern}")
        return value

    @field_validator("release_pointer")
    @classmethod
    def _validate_pointer(cls, value: str | None) -> str | None:
        """Reject pointer names outside the known set."""
        if value is not None and value not in _KNOWN_POINTERS:
            raise ValueError(f"invalid pointer {value!r}: must be one of {sorted(_KNOWN_POINTERS)}")
        return value

    @field_validator("expected_commit_sha")
    @classmethod
    def _validate_commit(cls, value: str | None) -> str | None:
        """Reject commit SHAs that are neither 40-char SHA-1 nor 64-char SHA-256."""
        if value is None:
            return None
        if _SHA1_RE.fullmatch(value) is None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError(f"invalid expected_commit_sha {value!r}: must be a 40-char SHA-1 or 64-char SHA-256 hex string")
        return value

    @field_validator("expected_slip_sha256")
    @classmethod
    def _validate_slip_sha(cls, value: str | None) -> str | None:
        """Reject slip SHAs that are not 64-char SHA-256 hex."""
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError(f"invalid expected_slip_sha256 {value!r}: must be a 64-char SHA-256 hex string")
        return value

    @model_validator(mode="after")
    def _check_combination(self) -> VerifyRequest:
        """Enforce exactly-one-of release_id / release_pointer."""
        if self.release_id is None and self.release_pointer is None:
            raise ValueError("exactly one of release_id or release_pointer is required")
        if self.release_id is not None and self.release_pointer is not None:
            raise ValueError("only one of release_id or release_pointer may be set")
        return self


class PublishRequest(VerifyRequest):
    """Body for POST /publish - identical shape to verify."""


class DeployRequest(VerifyRequest):
    """Body for POST /deploy - identical shape to verify."""


class VerifyResponse(BaseModel):
    """Response body for POST /verify."""

    status: Literal["ok", "blocked"]
    release_id: str | None
    content_sha256: str | None
    checks: dict[str, bool] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


class PublishResponse(VerifyResponse):
    """Response body for POST /publish."""

    receipt_id: str | None = None
    recorded_at_utc: str | None = None


class DeployResponse(BaseModel):
    """Response body for a successful POST /deploy.

    Failure cases never reach this shape: the plugin raises a typed
    :class:`MinecraftDaemonError` and the exception handler serves an
    :class:`ErrorResponse` with the appropriate ``code`` and HTTP status.
    """

    status: Literal["deployed"] = "deployed"
    release_id: str
    completed_at_utc: str


class StatusResponse(BaseModel):
    """Response body for GET /status."""

    daemon_name: str
    daemon_version: str
    active_release: str | None
    last_registry_head: str | None
    last_verification: dict | None
    health: dict


class ErrorResponse(BaseModel):
    """Error body for every MinecraftDaemonError."""

    code: int
    message: str
    detail: str | None = None
