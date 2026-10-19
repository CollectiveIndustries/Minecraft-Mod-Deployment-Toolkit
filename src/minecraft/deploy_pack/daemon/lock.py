# src/minecraft/deploy_pack/daemon/lock.py

"""Single exclusive deployment lock (spec v1.3 Section 12.6).

One lock file, one daemon, one deployment. The lock is acquired before
stage 11 of spec Section 12.4 (pre-stage runtime and artifacts) and released
after stage 18 (record the active release).

Lock file format
----------------
JSON with three fields: ``holder`` (a free-form identifier, usually the
run_id), ``pid`` (the daemon's PID), and ``acquired_at_utc``.

Atomic acquisition is implemented by creating a unique temporary file
in the same directory, writing the payload, fsyncing, and then
``os.link``-ing it into place. ``os.link`` fails with
``FileExistsError`` if the target exists, with no intermediate state in
which a partially-written lock file is visible. Two concurrent callers
cannot both succeed.

The temporary file is created with :func:`tempfile.mkstemp` so it is
unique per call even when multiple threads within the same process
race to acquire the lock. A shared ``<pid>`` suffix would cause the
losing thread to see ``FileNotFoundError`` (source removed by the
winner's cleanup) instead of ``FileExistsError`` (target exists).

Stale-lock detection
--------------------
A lock older than ``stale_after_seconds`` is *stale* and can be
force-released by the operator (via :meth:`force_release`). A stale lock
is never auto-released: spec Section 12.6 requires operator confirmation before
override. The daemon's normal startup path calls :meth:`is_stale` and,
if True, refuses to proceed until the operator runs :meth:`force_release`
explicitly.

The staleness decision is mtime-based. The ``pid`` field is
informational: it appears in the warning so the operator can
``ps -p`` it, but the daemon does not attempt to check PID liveness
itself (cross-platform PID checks are unreliable and the operator
confirmation requirement makes them unnecessary).

Malformed lock files
--------------------
A lock file that exists but cannot be parsed as JSON is treated as
stale by :meth:`is_stale` -- this can only happen if the file was
corrupted by external interference, since atomic acquisition guarantees
the file is complete once visible.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from LoggingCore import get_logger

from .errors import DeploymentLockHeldError

__all__ = ["DeploymentLock", "LockInfo"]


_log = get_logger(__name__)


@dataclass(frozen=True)
class LockInfo:
    """Parsed lock file contents."""

    holder: str
    pid: int
    acquired_at_utc: str
    mtime: float


def _now_utc() -> str:
    """Return the current UTC time as an ISO-8601 Z string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class DeploymentLock:
    """Exclusive filesystem lock for one deployment at a time."""

    def __init__(self, path: Path, *, stale_after_seconds: int = 3600) -> None:
        """Store the lock path and staleness threshold."""
        self._path = path
        self._stale_after = stale_after_seconds

    @property
    def path(self) -> Path:
        """Return the lock file path."""
        return self._path

    def read_holder(self) -> LockInfo | None:
        """Return the parsed lock file, or None when unlocked or malformed."""
        if not self._path.is_file():
            return None
        try:
            mtime = self._path.stat().st_mtime
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return LockInfo(
                holder=str(data.get("holder", "")),
                pid=int(data.get("pid", 0)),
                acquired_at_utc=str(data.get("acquired_at_utc", "")),
                mtime=mtime,
            )
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return None

    def is_stale(self, *, now: float | None = None) -> bool:
        """Return True if a lock exists and is stale or unreadable.

        An existing but unreadable lock file is treated as stale -- this
        is only possible from external corruption, since acquisition is
        atomic.
        """
        if not self._path.is_file():
            return False
        info = self.read_holder()
        if info is None:
            _log.warning(f"lock: unreadable lock file at {self._path}; treating as stale")
            return True
        effective_now = now if now is not None else time.time()
        return (effective_now - info.mtime) >= self._stale_after

    def _write_payload(self, fd: int, payload: bytes) -> None:
        """Write ``payload`` to ``fd`` and fsync it.

        ``fd`` is consumed by the ``with`` block; the caller must not
        close it again. ``fsync`` guarantees the payload reaches stable
        storage before the link makes it visible.
        """
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

    def _link_into_place(self, tmp: Path, holder: str) -> None:
        """Atomically link ``tmp`` into ``self._path``, raising on contention.

        Uses ``os.link`` so a partially-written lock file is never
        visible. On contention, raises :class:`DeploymentLockHeldError`
        with the current holder's identity for the operator diagnostic.
        """
        try:
            os.link(str(tmp), str(self._path))
        except FileExistsError as exc:
            info = self.read_holder()
            held_by = info.holder if info else "(unknown)"
            acquired = info.acquired_at_utc if info else "(unknown)"
            raise DeploymentLockHeldError(
                f"deployment lock already held at {self._path}",
                detail=f"holder={held_by!r} acquired_at_utc={acquired}",
            ) from exc

    def acquire(self, *, holder: str) -> None:
        """Atomically acquire the lock; raise DeploymentLockHeldError if held.

        Uses ``mkstemp-then-link``: the temporary file is unique per call,
        so two concurrent attempts (from separate threads or separate
        processes) never share a temp path. The link is the
        synchronization point; exactly one attempt succeeds.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "holder": holder,
                "pid": os.getpid(),
                "acquired_at_utc": _now_utc(),
            },
            sort_keys=True,
        ).encode("utf-8")
        fd, tmp_str = tempfile.mkstemp(
            prefix=f"{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        tmp = Path(tmp_str)
        try:
            self._write_payload(fd, payload)
            self._link_into_place(tmp, holder)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        _log.info(f"deployment lock acquired: {self._path} holder={holder!r}")

    def release(self) -> None:
        """Remove the lock file. A missing lock is a no-op."""
        try:
            self._path.unlink()
            _log.info(f"deployment lock released: {self._path}")
        except FileNotFoundError:
            _log.debug(f"deployment lock release: {self._path} already gone")

    def force_release(self) -> None:
        """Remove a stale lock; refuse if the lock is not stale.

        Operator-facing recovery path. Callers should invoke this only
        after confirming (via :meth:`read_holder` and :meth:`is_stale`)
        that the lock corresponds to a dead deployment.
        """
        if not self._path.is_file():
            _log.warning(f"force_release: no lock file at {self._path}; nothing to do")
            return
        if not self.is_stale():
            info = self.read_holder()
            detail = f"holder={info.holder!r}" if info else "(unreadable)"
            raise DeploymentLockHeldError(
                f"refusing to force-release a fresh lock at {self._path}",
                detail=detail,
            )
        info = self.read_holder()
        _log.warning(f"deployment lock force-released: {self._path} holder={info.holder if info else 'unknown'}")
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()

    @contextmanager
    def held(self, *, holder: str) -> Iterator[None]:
        """Context manager: acquire on enter, release on exit."""
        self.acquire(holder=holder)
        try:
            yield
        finally:
            self.release()
