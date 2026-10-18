# tests/modpack_upgrade/providers/test_http.py

"""RequestsTransport retry and size-guard tests."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import requests

from minecraft.modpack_upgrade.errors import ProviderError
from minecraft.modpack_upgrade.providers.http import RequestsTransport


class _FakeResponse:
    """Scripted ``requests`` response stand-in."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        content: bytes = b"body",
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"Content-Length": str(len(content))}
        self._chunks = chunks if chunks is not None else [content]
        self.closed = False

    def iter_content(self, chunk_size: int = 65536) -> Iterator[bytes]:
        """Yield the scripted chunks."""
        yield from self._chunks

    def close(self) -> None:
        """Mark the response as closed."""
        self.closed = True


class _FakeSession:
    """``requests.Session`` stand-in that pops one scripted outcome per call."""

    def __init__(self, scripted: list[Any]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        """Pop and return the next scripted outcome; raise if it is an exception."""
        self.calls.append({"url": url, **kwargs})
        if not self._scripted:
            raise AssertionError(f"no scripted response for {url}")
        outcome = self._scripted.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _transport(session: _FakeSession, **overrides: Any) -> tuple[RequestsTransport, list[float]]:
    """Return a transport wired to ``session`` and a list capturing sleeps."""
    sleeps: list[float] = []
    defaults: dict[str, Any] = {
        "session": session,
        "timeout": 5.0,
        "max_retries": 3,
        "backoff_base": 0.5,
        "sleep_fn": sleeps.append,
    }
    defaults.update(overrides)
    return RequestsTransport(**defaults), sleeps


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_get_returns_2xx_immediately() -> None:
    """A 200 returns without retries."""
    session = _FakeSession([_FakeResponse(content=b"hi")])
    transport, sleeps = _transport(session)
    result = transport.get("https://x/y")
    assert result.status_code == 200
    assert result.content == b"hi"
    assert sleeps == []
    assert len(session.calls) == 1


def test_get_passes_headers_and_timeout() -> None:
    """Caller headers and timeout reach the session."""
    session = _FakeSession([_FakeResponse()])
    transport, _ = _transport(session)
    transport.get("https://x/y", headers={"User-Agent": "test"}, timeout=1.5)
    call = session.calls[0]
    assert call["headers"] == {"User-Agent": "test"}
    assert call["timeout"] == 1.5


def test_headers_lowercased_in_result() -> None:
    """Response header names come back lowercased."""
    session = _FakeSession([_FakeResponse(headers={"Content-Type": "application/json"})])
    transport, _ = _transport(session)
    result = transport.get("https://x/y")
    assert result.headers == {"content-type": "application/json"}


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def test_5xx_is_retried_then_succeeds() -> None:
    """A 5xx is retried; the following 200 succeeds."""
    session = _FakeSession([_FakeResponse(status_code=500), _FakeResponse(content=b"ok")])
    transport, sleeps = _transport(session)
    result = transport.get("https://x/y")
    assert result.status_code == 200
    assert len(session.calls) == 2
    assert sleeps == [0.5]


def test_5xx_exhausts_retries_returns_last_response() -> None:
    """After ``max_retries`` extra attempts, the last 5xx response is returned.

    The transport does not raise on HTTP status - adapters attach their
    own semantics to a returned status code. Network errors are the only
    thing raised from the transport (see the network-error tests below).
    """
    session = _FakeSession([_FakeResponse(status_code=503) for _ in range(4)])
    transport, sleeps = _transport(session, max_retries=3)
    result = transport.get("https://x/y")
    assert result.status_code == 503
    assert len(session.calls) == 4
    assert sleeps == [0.5, 1.0, 2.0]


def test_429_is_retried() -> None:
    """429 is retriable (rate limit)."""
    session = _FakeSession([_FakeResponse(status_code=429), _FakeResponse(content=b"ok")])
    transport, _ = _transport(session)
    result = transport.get("https://x/y")
    assert result.status_code == 200


@pytest.mark.parametrize("status", [400, 401, 403, 404, 418])
def test_4xx_other_than_429_is_not_retried(status: int) -> None:
    """4xx other than 429 returns immediately without sleeping."""
    session = _FakeSession([_FakeResponse(status_code=status, content=b"err")])
    transport, sleeps = _transport(session)
    result = transport.get("https://x/y")
    assert result.status_code == status
    assert sleeps == []
    assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------


def test_network_error_is_retried() -> None:
    """A requests-level connection error is retried."""
    session = _FakeSession([requests.exceptions.ConnectionError("boom"), _FakeResponse(content=b"ok")])
    transport, sleeps = _transport(session)
    result = transport.get("https://x/y")
    assert result.status_code == 200
    assert len(sleeps) == 1


def test_network_error_exhausts_retries() -> None:
    """Repeated network errors eventually raise ProviderError."""
    session = _FakeSession([requests.exceptions.ConnectionError("boom") for _ in range(4)])
    transport, _ = _transport(session, max_retries=3)
    with pytest.raises(ProviderError, match="network error"):
        transport.get("https://x/y")


# ---------------------------------------------------------------------------
# Size guard
# ---------------------------------------------------------------------------


def test_max_bytes_enforced_during_streaming() -> None:
    """A body that exceeds max_bytes raises and closes the response."""
    big = _FakeResponse(chunks=[b"a" * 100, b"b" * 100, b"c" * 100])
    session = _FakeSession([big])
    transport, _ = _transport(session)
    with pytest.raises(ProviderError, match="max_bytes"):
        transport.get("https://x/y", max_bytes=250)
    assert big.closed


def test_max_bytes_allows_bodies_under_limit() -> None:
    """A body that fits under max_bytes is returned intact."""
    big = _FakeResponse(chunks=[b"a" * 100, b"b" * 100])
    session = _FakeSession([big])
    transport, _ = _transport(session)
    result = transport.get("https://x/y", max_bytes=1000)
    assert result.content == b"a" * 100 + b"b" * 100


def test_no_max_bytes_returns_full_content() -> None:
    """When max_bytes is None, the non-streamed content is returned."""
    session = _FakeSession([_FakeResponse(content=b"whole")])
    transport, _ = _transport(session)
    result = transport.get("https://x/y")
    assert result.content == b"whole"
