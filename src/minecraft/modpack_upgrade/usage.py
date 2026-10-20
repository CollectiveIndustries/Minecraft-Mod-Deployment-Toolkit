# src/minecraft/modpack_upgrade/usage.py

"""API and download budget tracking (spec v1.4.0 Section 8.2.1).

Records every provider API call and every artifact download to a
JSONL usage log, and enforces configurable windows and caps.

Design notes
------------
- Append-only JSONL, same pattern as ``release-index.jsonl``,
  ``operation.jsonl``, and the shipping backup journal.
- Fsync per record: durability over throughput. A resolve makes 200-400
  calls; a few hundred fsyncs add <2 seconds to a run.
- Records classified as ``api`` or ``download`` by URL hostname.
  API calls hit the provider's metadata endpoint and consume the API
  budget. Downloads hit the CDN and consume the byte budget.
- Read is backward-from-EOF, stopping at the window boundary. For a
  rolling 24h window with 5000 calls, reads only the log's suffix.
- Caps default to ``None`` (no enforcement). Operators opt in via
  ``[budget]`` in ``upgrade.toml``.
- Single-writer assumption: v1.4.0 is single-DEV. Concurrent writers
  (e.g., two ``update`` invocations) may interleave and slightly exceed
  the cap. Multi-writer coordination is deferred to v2.0.0.
"""

from __future__ import annotations

import contextvars
import json
import os
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from LoggingCore import get_logger

from .errors import ApprovalBlockedError, UpgradeError

__all__ = [
    "UsageCapExceeded",
    "UsageConfig",
    "UsageLogReadError",
    "UsageTracker",
    "load_usage_config",
    "run_context",
]


_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UsageCapExceeded(ApprovalBlockedError):
    """A configured API or byte budget has been exhausted for the window."""

    def __init__(self, kind: str, used: int, cap: int) -> None:
        """Store the exhaustion context and build the message."""
        self.kind = kind
        self.used = used
        self.cap = cap
        super().__init__(f"{kind} budget exhausted for the current window: used={used} cap={cap}")


class UsageLogReadError(UpgradeError):
    """The usage log file could not be read."""

    exit_code = 3


# ---------------------------------------------------------------------------
# Run context (for correlating log records with a run)
# ---------------------------------------------------------------------------


_run_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("usage_run_id", default=None)


@contextmanager
def run_context(run_id: str) -> Iterator[None]:
    """Set the current run's ID for the duration of the block.

    Records written inside the block carry the ``run_id``. Records
    written outside do not. Cap enforcement does not depend on this
    field; it exists for post-hoc debugging.
    """
    token = _run_id_ctx.set(run_id)
    try:
        yield
    finally:
        _run_id_ctx.reset(token)


def _current_run_id() -> str | None:
    """Return the current run ID from the context, or ``None``."""
    return _run_id_ctx.get()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageConfig:
    """Fully-resolved usage-tracking configuration.

    ``max_api_requests`` and ``max_downloaded_bytes`` are independent
    caps, both optional. When either is ``None``, that dimension is
    unbounded. When both are ``None``, no usage log is written at all.
    """

    log_path: Path
    window_hours: int = 24
    max_api_requests: int | None = None
    max_downloaded_bytes: int | None = None

    @property
    def enabled(self) -> bool:
        """True iff at least one cap is configured."""
        return self.max_api_requests is not None or self.max_downloaded_bytes is not None


def _parse_optional_non_negative_int(raw: object, name: str) -> int | None:
    """Parse an optional non-negative integer; log and return None on error."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        _log.warning(f"usage: [budget].{name} is invalid: {raw!r}")
        return None
    return raw


def load_usage_config(config_dir: Path, project_root: Path) -> UsageConfig | None:
    """Read the ``[budget]`` section from ``upgrade.toml``.

    Returns ``None`` when the section is absent, malformed, or
    configures no caps. A ``[budget]`` block with ``usage_log_path``
    set but no caps is also ``None`` -- without a cap there is nothing
    to enforce.
    """
    toml_path = config_dir / "upgrade.toml"
    if not toml_path.is_file():
        return None
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _log.warning(f"usage: could not read {toml_path}: {exc}")
        return None
    section = data.get("budget")
    if not isinstance(section, dict):
        return None

    max_api = _parse_optional_non_negative_int(section.get("max_api_requests"), "max_api_requests")
    max_bytes = _parse_optional_non_negative_int(section.get("max_downloaded_bytes"), "max_downloaded_bytes")
    if max_api is None and max_bytes is None:
        return None

    log_raw = section.get("usage_log_path")
    if log_raw is None:
        log_path = project_root / "var" / "usage.jsonl"
    else:
        p = Path(str(log_raw))
        log_path = p if p.is_absolute() else project_root / p

    window_raw = section.get("usage_window_hours", 24)
    if isinstance(window_raw, bool) or not isinstance(window_raw, int) or window_raw <= 0:
        window_raw = 24

    return UsageConfig(
        log_path=log_path,
        window_hours=window_raw,
        max_api_requests=max_api,
        max_downloaded_bytes=max_bytes,
    )


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    """Return the current UTC time with microsecond precision and Z suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class UsageTracker:
    """Append-only usage log plus cap enforcement.

    Single-writer assumption: for v1.4.0 one process writes to the log
    at a time. Concurrent writers (e.g., two ``update`` invocations)
    may interleave and lose updates. Multi-writer coordination is
    deferred to v2.0.0.
    """

    def __init__(self, config: UsageConfig) -> None:
        """Store the config; no I/O until the first record or check."""
        self._config = config

    @property
    def config(self) -> UsageConfig:
        """Return the tracker's config."""
        return self._config

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _append(self, record: dict) -> None:
        """Append one JSON line and fsync."""
        self._config.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True) + "\n"
        with open(self._config.log_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def record_api_call(self, *, provider: str, endpoint: str) -> None:
        """Record one API call. Called after the request completes."""
        record: dict = {
            "timestamp_utc": _now_utc_iso(),
            "kind": "api",
            "provider": provider,
            "endpoint": endpoint,
        }
        run_id = _current_run_id()
        if run_id is not None:
            record["run_id"] = run_id
        self._append(record)

    def record_download(self, *, provider: str, url: str, bytes_count: int) -> None:
        """Record one download with its byte size. Called after the body is read."""
        record: dict = {
            "timestamp_utc": _now_utc_iso(),
            "kind": "download",
            "provider": provider,
            "url": url,
            "bytes": bytes_count,
        }
        run_id = _current_run_id()
        if run_id is not None:
            record["run_id"] = run_id
        self._append(record)

    # ------------------------------------------------------------------
    # Enforcement
    # ------------------------------------------------------------------

    def _window_start(self) -> datetime:
        """Return the oldest timestamp still in the rolling window."""
        return datetime.now(UTC) - timedelta(hours=self._config.window_hours)

    def _read_records_since(self, since: datetime) -> list[dict]:
        """Read records newer than ``since``, backward from EOF.

        Stops at the first record older than ``since``. Malformed lines
        are skipped; an unreadable file raises
        :class:`UsageLogReadError` (fails closed).
        """
        path = self._config.log_path
        if not path.is_file():
            return []
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        records: list[dict] = []
        chunk_size = 8192
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
                buffer = b""
                done = False
                while pos > 0 and not done:
                    read_size = min(chunk_size, pos)
                    pos -= read_size
                    f.seek(pos)
                    chunk = f.read(read_size)
                    buffer = chunk + buffer
                    lines = buffer.split(b"\n")
                    buffer = lines[0]
                    for line in reversed(lines[1:]):
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(record, dict):
                            continue
                        ts = record.get("timestamp_utc")
                        if not isinstance(ts, str) or ts < since_iso:
                            done = True
                            break
                        records.append(record)
                if not done and buffer:
                    try:
                        record = json.loads(buffer)
                        if isinstance(record, dict):
                            ts = record.get("timestamp_utc")
                            if isinstance(ts, str) and ts >= since_iso:
                                records.append(record)
                    except json.JSONDecodeError:
                        pass
        except OSError as exc:
            raise UsageLogReadError(f"could not read usage log {path}: {exc}") from exc
        return records

    def _count_api_since(self, since: datetime) -> int:
        """Count ``api`` records newer than ``since``."""
        return sum(1 for r in self._read_records_since(since) if r.get("kind") == "api")

    def _sum_bytes_since(self, since: datetime) -> int:
        """Sum download bytes in records newer than ``since``."""
        total = 0
        for r in self._read_records_since(since):
            if r.get("kind") == "download":
                b = r.get("bytes", 0)
                if isinstance(b, int) and not isinstance(b, bool):
                    total += b
        return total

    def check_api_cap(self) -> None:
        """Raise :class:`UsageCapExceeded` if the API cap is reached.

        Called before each API call. Uses ``>=`` so that at cap N, the
        (N+1)th call is refused.
        """
        if self._config.max_api_requests is None:
            return
        used = self._count_api_since(self._window_start())
        if used >= self._config.max_api_requests:
            raise UsageCapExceeded("api_request", used, self._config.max_api_requests)

    def check_byte_cap(self) -> None:
        """Raise :class:`UsageCapExceeded` if the byte cap is reached.

        Called before each download. Uses ``>=`` so that at cap N bytes,
        the next download is refused. A single download can slightly
        exceed the cap; the *next* check catches it.
        """
        if self._config.max_downloaded_bytes is None:
            return
        used = self._sum_bytes_since(self._window_start())
        if used >= self._config.max_downloaded_bytes:
            raise UsageCapExceeded("downloaded_bytes", used, self._config.max_downloaded_bytes)

    def api_used(self) -> int:
        """Return current API usage count in the window (diagnostics)."""
        return self._count_api_since(self._window_start())

    def bytes_used(self) -> int:
        """Return current byte usage in the window (diagnostics)."""
        return self._sum_bytes_since(self._window_start())
