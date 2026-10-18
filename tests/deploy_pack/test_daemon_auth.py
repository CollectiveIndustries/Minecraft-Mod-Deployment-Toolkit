# tests/deploy_pack/test_daemon_auth.py

"""Minecraft daemon authenticator tests."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from minecraft.deploy_pack.daemon.auth import (
    AuthFailedError,
    AuthForbiddenError,
    BearerAuth,
    CombinedAuth,
    TailscaleAuth,
)


def _make_request(client_ip: str, authorization: str | None = None) -> Request:
    """Build a minimal Starlette Request with a synthetic client and headers."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "client": (client_ip, 0),
    }
    if authorization is not None:
        scope["headers"] = [(b"authorization", authorization.encode())]
    return Request(scope)


def test_bearer_success():
    """A valid bearer token authenticates."""
    auth = BearerAuth(token="secret")
    identity = auth.authenticate(_make_request("10.0.0.1", "Bearer secret"))
    assert identity.principal.startswith("bearer:")


def test_bearer_missing_header():
    """No Authorization header raises AuthFailedError."""
    auth = BearerAuth(token="secret")
    with pytest.raises(AuthFailedError):
        auth.authenticate(_make_request("10.0.0.1", None))


def test_bearer_wrong_scheme():
    """A non-Bearer scheme raises AuthFailedError."""
    auth = BearerAuth(token="secret")
    with pytest.raises(AuthFailedError):
        auth.authenticate(_make_request("10.0.0.1", "Basic abc"))


def test_bearer_wrong_token():
    """A valid scheme with the wrong token raises AuthFailedError."""
    auth = BearerAuth(token="secret")
    with pytest.raises(AuthFailedError):
        auth.authenticate(_make_request("10.0.0.1", "Bearer wrong"))


def test_bearer_token_env(monkeypatch: pytest.MonkeyPatch):
    """The env-var path is used when token_env is provided."""
    monkeypatch.setenv("TEST_DAEMON_TOKEN", "from-env")
    auth = BearerAuth(token_env="TEST_DAEMON_TOKEN")
    identity = auth.authenticate(_make_request("10.0.0.1", "Bearer from-env"))
    assert identity.principal.startswith("bearer:")


def test_bearer_token_env_missing(monkeypatch: pytest.MonkeyPatch):
    """An unset env var raises at construction time."""
    monkeypatch.delenv("TEST_DAEMON_TOKEN", raising=False)
    with pytest.raises(ValueError, match="environment variable"):
        BearerAuth(token_env="TEST_DAEMON_TOKEN")


def test_bearer_requires_source():
    """Constructing without token or token_env raises."""
    with pytest.raises(ValueError):
        BearerAuth()


def test_bearer_rejects_both_sources():
    """Constructing with both token and token_env raises."""
    with pytest.raises(ValueError):
        BearerAuth(token="x", token_env="Y")


def test_tailscale_accepts_in_range():
    """A client IP in range authenticates."""
    auth = TailscaleAuth(cidrs=("100.64.0.0/10",))
    identity = auth.authenticate(_make_request("100.64.1.1", None))
    assert identity.principal.startswith("tailscale:")


def test_tailscale_rejects_out_of_range():
    """A client IP outside all CIDRs raises AuthForbiddenError."""
    auth = TailscaleAuth(cidrs=("100.64.0.0/10",))
    with pytest.raises(AuthForbiddenError):
        auth.authenticate(_make_request("192.168.1.1", None))


def test_tailscale_rejects_invalid_ip():
    """A malformed source IP raises AuthForbiddenError."""
    auth = TailscaleAuth(cidrs=("100.64.0.0/10",))
    with pytest.raises(AuthForbiddenError):
        auth.authenticate(_make_request("not-an-ip", None))


def test_tailscale_requires_cidrs():
    """Constructing with an empty CIDR tuple raises."""
    with pytest.raises(ValueError):
        TailscaleAuth(cidrs=())


def test_tailscale_reads_user_header():
    """The Tailscale-User-Login header is used for the principal."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"tailscale-user-login", b"alice@example.com")],
        "client": ("100.64.1.1", 0),
    }
    auth = TailscaleAuth(cidrs=("100.64.0.0/10",))
    identity = auth.authenticate(Request(scope))
    assert identity.principal == "tailscale:alice@example.com"


def test_combined_concatenates_principals():
    """CombinedAuth concatenates principals from every layer."""
    combined = CombinedAuth(
        (
            TailscaleAuth(cidrs=("100.64.0.0/10",)),
            BearerAuth(token="secret"),
        )
    )
    identity = combined.authenticate(_make_request("100.64.1.1", "Bearer secret"))
    assert identity.principal.startswith("tailscale:")
    assert "+bearer:" in identity.principal


def test_combined_rejects_on_any_layer():
    """CombinedAuth fails if any layer fails."""
    combined = CombinedAuth(
        (
            TailscaleAuth(cidrs=("100.64.0.0/10",)),
            BearerAuth(token="secret"),
        )
    )
    with pytest.raises(AuthForbiddenError):
        combined.authenticate(_make_request("192.168.1.1", "Bearer secret"))
    with pytest.raises(AuthFailedError):
        combined.authenticate(_make_request("100.64.1.1", "Bearer wrong"))


def test_combined_requires_layers():
    """Constructing with zero layers raises."""
    with pytest.raises(ValueError):
        CombinedAuth(())
