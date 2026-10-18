# src/minecraft/deploy_pack/daemon/idempotency.py

"""SQLite-backed idempotency ledger for the HTTP admin plugin.

Every POST requires ``Idempotency-Key: <opaque-token>``. Repeats with the
same key and body return the cached response. Repeats with a different
body raise :class:`IdempotencyConflictError`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import IdempotencyConflictError

__all__ = ["SQLiteIdempotency"]


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
)
"""


def _now_iso() -> str:
    """Return the current UTC time with microsecond precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SQLiteIdempotency:
    """Idempotency ledger stored in a SQLite database."""

    def __init__(self, path: Path | str, *, ttl_hours: int = 24) -> None:
        """Open or create the ledger at ``path``."""
        self._path = Path(path)
        self._ttl = timedelta(hours=ttl_hours)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE)
            conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a short-lived SQLite connection under the process lock."""
        with self._lock:
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            try:
                yield conn
            finally:
                conn.close()

    def _cutoff_iso(self) -> str:
        """Return the ISO timestamp marking the oldest non-expired row.

        Microsecond precision so that ``ttl_hours=0`` (and any sub-second
        TTL) prunes correctly even when two rows are written within the
        same wall-clock second.
        """
        return (datetime.now(UTC) - self._ttl).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _prune_expired(self, conn: sqlite3.Connection) -> None:
        """Delete rows older than the configured TTL."""
        conn.execute(
            "DELETE FROM idempotency WHERE created_at_utc < ?",
            (self._cutoff_iso(),),
        )

    def lookup(self, key: str, endpoint: str, request_hash: str) -> tuple[int, dict[str, Any]] | None:
        """Return the cached (status, body) for ``key``, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT endpoint, request_hash, response_status, response_body FROM idempotency WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        stored_endpoint, stored_hash, status, body_text = row
        if stored_endpoint != endpoint or stored_hash != request_hash:
            raise IdempotencyConflictError(
                f"Idempotency-Key {key!r} was reused with a different request",
                detail=(f"stored_endpoint={stored_endpoint!r} current_endpoint={endpoint!r}"),
            )
        return (int(status), json.loads(body_text))

    def store(
        self,
        key: str,
        endpoint: str,
        request_hash: str,
        status: int,
        body: dict[str, Any],
    ) -> None:
        """Record the response for ``key`` and prune expired rows."""
        timestamp = _now_iso()
        with self._connect() as conn:
            self._prune_expired(conn)
            conn.execute(
                "INSERT OR REPLACE INTO idempotency (key, endpoint, request_hash, response_status, response_body, created_at_utc) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    key,
                    endpoint,
                    request_hash,
                    status,
                    json.dumps(body, sort_keys=True),
                    timestamp,
                ),
            )
            conn.commit()
