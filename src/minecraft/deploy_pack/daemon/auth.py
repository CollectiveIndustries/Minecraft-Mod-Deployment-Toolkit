# src/minecraft/deploy_pack/daemon/auth.py

"""HTTP authenticators for the Minecraft deploy daemon.

DaemonCore does not ship authenticators - auth is HTTP-specific. This
module provides BearerAuth, TailscaleAuth, and CombinedAuth for the
Minecraft plugin's use. If another application needs the same, its
author decides whether to copy this file, import it from minecraft, or
extract it into a shared package.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from dataclasses import dataclass
from typing import Protocol

from .errors import MinecraftDaemonError

__all__ = [
    "AuthFailedError",
    "AuthForbiddenError",
    "AuthIdentity",
    "Authenticator",
    "BearerAuth",
    "CombinedAuth",
    "TailscaleAuth",
]


class AuthFailedError(MinecraftDaemonError):
    """Authentication failed. HTTP 401, code 28."""

    http_status = 401
    code = 28


class AuthForbiddenError(MinecraftDaemonError):
    """Authenticated but not authorized. HTTP 403, code 29."""

    http_status = 403
    code = 29


@dataclass(frozen=True)
class AuthIdentity:
    """Identity of the caller for audit records."""

    principal: str


class Authenticator(Protocol):
    """Verifies a request's identity. Raises on failure."""

    def authenticate(self, request: object) -> AuthIdentity:
        """Return the caller's identity or raise."""
        ...


def _extract_bearer(authorization: str | None) -> str:
    """Return the bearer token from an ``Authorization`` header, or raise."""
    if not authorization:
        raise AuthFailedError("missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise AuthFailedError(
            "Authorization scheme must be Bearer",
            detail=f"scheme={scheme!r}",
        )
    if not token:
        raise AuthFailedError("empty bearer token")
    return token


@dataclass(frozen=True)
class BearerAuth:
    """Static bearer-token authentication.

    The token may be supplied directly or read from an environment
    variable at construction time (recommended so it never lands in
    tracked configuration).
    """

    token_env: str | None = None
    token: str | None = None

    def __post_init__(self) -> None:
        """Validate that exactly one token source was provided and non-empty."""
        if self.token is None and self.token_env is None:
            raise ValueError("BearerAuth requires either token or token_env")
        if self.token is not None and self.token_env is not None:
            raise ValueError("BearerAuth accepts only one of token or token_env")
        if self.token_env is not None:
            resolved = os.environ.get(self.token_env, "")
            if not resolved:
                raise ValueError(f"BearerAuth: environment variable {self.token_env!r} is not set or empty")
            object.__setattr__(self, "token", resolved)

    def authenticate(self, request: object) -> AuthIdentity:
        """Validate the bearer token; raise on any mismatch."""
        headers = getattr(request, "headers", {})
        token = _extract_bearer(headers.get("authorization"))
        assert self.token is not None
        if not hmac.compare_digest(token, self.token):
            raise AuthFailedError("bearer token is invalid")
        token_hash = hmac.new(self.token.encode(), b"minecraft-daemon", "sha256").hexdigest()[:16]
        return AuthIdentity(principal=f"bearer:{token_hash}")


@dataclass(frozen=True)
class TailscaleAuth:
    """Source-IP authentication against one or more CIDR ranges.

    Also reads ``Tailscale-User-Login`` (populated by Tailscale serve)
    into the principal when present.
    """

    cidrs: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject an empty CIDR list - that would authenticate everyone."""
        if not self.cidrs:
            raise ValueError("TailscaleAuth requires at least one CIDR")

    def _in_range(self, ip_str: str) -> bool:
        """Return True when ``ip_str`` is inside one of the CIDRs."""
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        for cidr in self.cidrs:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                continue
            if ip in net:
                return True
        return False

    def authenticate(self, request: object) -> AuthIdentity:
        """Validate the source IP; raise on any mismatch."""
        client = getattr(request, "client", None)
        client_ip = getattr(client, "host", "") if client else ""
        if not self._in_range(client_ip):
            raise AuthForbiddenError(
                "source IP is not in the allowed Tailscale range",
                detail=f"client_ip={client_ip}",
            )
        headers = getattr(request, "headers", {})
        user = headers.get("tailscale-user-login", "")
        if user:
            return AuthIdentity(principal=f"tailscale:{user}")
        return AuthIdentity(principal=f"tailscale:{client_ip}")


@dataclass(frozen=True)
class CombinedAuth:
    """Requires every layer to pass; concatenates principals."""

    authenticators: tuple[Authenticator, ...]

    def __post_init__(self) -> None:
        """Reject an empty authenticator list."""
        if not self.authenticators:
            raise ValueError("CombinedAuth requires at least one authenticator")

    def authenticate(self, request: object) -> AuthIdentity:
        """Run every authenticator; combine principals on success."""
        principals: list[str] = []
        for authenticator in self.authenticators:
            identity = authenticator.authenticate(request)
            principals.append(identity.principal)
        return AuthIdentity(principal="+".join(principals))
