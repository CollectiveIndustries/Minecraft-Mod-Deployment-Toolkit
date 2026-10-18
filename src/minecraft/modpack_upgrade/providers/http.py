# src/minecraft/modpack_upgrade/providers/http.py

"""Production HTTP transport for provider adapters.

Wraps :mod:`requests` with the two behaviours every provider needs:

  * **Bounded retries with exponential backoff** on transient
    failures (5xx, connection errors, timeouts). Never retries 4xx -
    those are permanent (Section 18.4).
  * **Bounded response size.** When ``max_bytes`` is set the body is
    streamed and the connection is dropped the moment the limit is
    crossed, so an attacker-controlled provider cannot exhaust memory
    before the caller sees a diagnostic.

The transport is deliberately dumb: it returns every HTTP response,
including 4xx and 5xx, so adapters can attach their own semantics
(CurseForge 401 -> missing/expired key, Modrinth 429 -> rate limit).
Only network-level failures are raised from here.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests

from minecraft.modpack_upgrade.errors import ProviderError

from .base import HttpResult

__all__ = ["RequestsTransport"]

_DEFAULT_TIMEOUT: float = 30.0
_DEFAULT_MAX_RETRIES: int = 3
_DEFAULT_BACKOFF_BASE: float = 1.0
_STREAM_CHUNK: int = 65536


def _lowercase_headers(raw: Any) -> dict[str, str]:
    """Return response headers with lowercased names."""
    try:
        return {str(k).lower(): str(v) for k, v in raw.items()}
    except AttributeError:
        return {}


class _BytesResponse:
    """Adapter around a :mod:`requests` response so the size guard runs once."""

    def __init__(self, response: Any, max_bytes: int | None) -> None:
        self._response = response
        self._max_bytes = max_bytes

    def status_code(self) -> int:
        """Return the HTTP status code."""
        return int(self._response.status_code)

    def headers(self) -> dict[str, str]:
        """Return lowercased response headers."""
        return _lowercase_headers(self._response.headers)

    def content(self) -> bytes:
        """Return the body, streaming and enforcing ``max_bytes`` if set."""
        if self._max_bytes is None:
            return self._response.content
        collected: list[bytes] = []
        total = 0
        for chunk in self._response.iter_content(chunk_size=_STREAM_CHUNK):
            if not chunk:
                continue
            total += len(chunk)
            if total > self._max_bytes:
                self._response.close()
                raise ProviderError(f"response exceeded max_bytes={self._max_bytes} (downloaded at least {total} bytes)")
            collected.append(chunk)
        return b"".join(collected)


class RequestsTransport:
    """HTTP transport backed by :mod:`requests`.

    Retries transient failures (5xx, connection errors, timeouts) with
    exponential backoff. A ``sleep_fn`` is injectable so tests do not
    actually sleep. ``session`` is likewise injectable so tests can
    script responses without sockets.
    """

    def __init__(
        self,
        *,
        session: Any = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """Store the tunables; no network activity until :meth:`get`."""
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep_fn

    def _attempt(self, url: str, headers: dict[str, str] | None, timeout: float, max_bytes: int | None) -> _BytesResponse:
        """Perform one GET attempt; raise :class:`ProviderError` on network failure."""
        try:
            response = self._session.get(url, headers=headers, timeout=timeout, stream=max_bytes is not None)
        except requests.exceptions.RequestException as exc:
            raise ProviderError(f"network error during GET {url}: {exc}") from exc
        return _BytesResponse(response, max_bytes)

    @staticmethod
    def _is_retriable_status(status_code: int) -> bool:
        """True for 5xx and 429; False for everything else including other 4xx."""
        if status_code == 429:
            return True
        return 500 <= status_code < 600

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff: base * 2^attempt."""
        return self._backoff_base * (2**attempt)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> HttpResult:
        """Perform a GET with bounded retries and optional size limit.

        Retries on network errors and 5xx/429 up to ``max_retries``
        additional attempts. Every other status is returned immediately.
        """
        effective_timeout = timeout if timeout is not None else self._timeout
        last_error: ProviderError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._attempt(url, headers, effective_timeout, max_bytes)
            except ProviderError as exc:
                last_error = exc
            else:
                status = response.status_code()
                if not self._is_retriable_status(status) or attempt == self._max_retries:
                    return HttpResult(
                        status_code=status,
                        content=response.content(),
                        headers=response.headers(),
                    )
                last_error = ProviderError(f"retriable HTTP {status} from {url}")
            if attempt < self._max_retries:
                self._sleep(self._backoff_seconds(attempt))
        assert last_error is not None
        raise last_error
