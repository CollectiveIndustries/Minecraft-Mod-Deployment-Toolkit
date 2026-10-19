# tests/deploy_pack/test_daemon_lock.py

"""Unit tests for ``daemon.lock`` (spec v1.3 Section 12.6).

Covers atomic acquisition, holder introspection, staleness detection,
operator force-release, context-manager lifecycle, and concurrent
acquisition (exactly one winner under a thread barrier).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from minecraft.deploy_pack.daemon.errors import DeploymentLockHeldError
from minecraft.deploy_pack.daemon.lock import DeploymentLock

# ---------------------------------------------------------------------------
# Acquire / release
# ---------------------------------------------------------------------------


def test_acquire_creates_file_with_holder(tmp_path: Path) -> None:
    """Acquire writes the holder, PID, and timestamp into the lock file."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    lock.acquire(holder="run-42")
    assert lock.path.is_file()
    payload = json.loads(lock.path.read_text(encoding="utf-8"))
    assert payload["holder"] == "run-42"
    assert isinstance(payload["pid"], int)
    assert payload["acquired_at_utc"].endswith("Z")


def test_acquire_creates_parent_directories(tmp_path: Path) -> None:
    """Acquire creates the parent directory chain when missing."""
    lock = DeploymentLock(tmp_path / "nested" / "dir" / "deploy.lock")
    lock.acquire(holder="run-1")
    assert lock.path.is_file()


def test_acquire_raises_when_held(tmp_path: Path) -> None:
    """A second acquire raises with the current holder in the detail."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    lock.acquire(holder="run-a")
    with pytest.raises(DeploymentLockHeldError) as ei:
        lock.acquire(holder="run-b")
    assert "run-a" in (ei.value.detail or "")


def test_release_removes_file(tmp_path: Path) -> None:
    """Release removes the lock file."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    lock.acquire(holder="run-1")
    lock.release()
    assert not lock.path.is_file()


def test_release_missing_is_noop(tmp_path: Path) -> None:
    """Releasing an unlocked lock is a silent no-op."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    lock.release()
    assert not lock.path.is_file()


# ---------------------------------------------------------------------------
# Holder introspection
# ---------------------------------------------------------------------------


def test_read_holder_none_when_unlocked(tmp_path: Path) -> None:
    """``read_holder`` returns None when no file exists."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    assert lock.read_holder() is None


def test_read_holder_returns_info(tmp_path: Path) -> None:
    """``read_holder`` returns the parsed lock contents with mtime."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    lock.acquire(holder="run-7")
    info = lock.read_holder()
    assert info is not None
    assert info.holder == "run-7"
    assert info.mtime > 0


def test_read_holder_none_when_corrupt(tmp_path: Path) -> None:
    """A malformed lock file yields None from ``read_holder``."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    lock.path.write_text("not json\n", encoding="utf-8")
    assert lock.read_holder() is None


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_is_stale_false_when_unlocked(tmp_path: Path) -> None:
    """An unlocked path is not stale."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    assert lock.is_stale() is False


def test_is_stale_false_for_fresh_lock(tmp_path: Path) -> None:
    """A freshly acquired lock is not stale."""
    lock = DeploymentLock(tmp_path / "deploy.lock", stale_after_seconds=3600)
    lock.acquire(holder="run-1")
    assert lock.is_stale() is False


def test_is_stale_true_when_mtime_old(tmp_path: Path) -> None:
    """A lock older than the threshold is stale."""
    lock = DeploymentLock(tmp_path / "deploy.lock", stale_after_seconds=1)
    lock.acquire(holder="run-1")
    # Force mtime into the past.
    assert lock.is_stale(now=time.time() + 10) is True


def test_is_stale_true_for_corrupt_file(tmp_path: Path) -> None:
    """A corrupt lock file is treated as stale."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    lock.path.write_text("not json\n", encoding="utf-8")
    assert lock.is_stale() is True


# ---------------------------------------------------------------------------
# Force release
# ---------------------------------------------------------------------------


def test_force_release_removes_stale_lock(tmp_path: Path) -> None:
    """``force_release`` removes a lock older than the threshold."""
    lock = DeploymentLock(tmp_path / "deploy.lock", stale_after_seconds=1)
    lock.acquire(holder="run-1")
    time.sleep(1.1)
    lock.force_release()
    assert not lock.path.is_file()


def test_force_release_refuses_fresh_lock(tmp_path: Path) -> None:
    """``force_release`` on a fresh lock raises DeploymentLockHeldError."""
    lock = DeploymentLock(tmp_path / "deploy.lock", stale_after_seconds=3600)
    lock.acquire(holder="run-1")
    with pytest.raises(DeploymentLockHeldError):
        lock.force_release()


def test_force_release_missing_is_noop(tmp_path: Path) -> None:
    """``force_release`` on a missing lock is a silent no-op."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    lock.force_release()
    assert not lock.path.is_file()


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_held_context_releases_on_exit(tmp_path: Path) -> None:
    """The ``held`` context manager releases on normal exit."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    with lock.held(holder="run-1"):
        assert lock.path.is_file()
    assert not lock.path.is_file()


def test_held_context_releases_on_exception(tmp_path: Path) -> None:
    """The ``held`` context manager releases even when the body raises."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    with pytest.raises(RuntimeError), lock.held(holder="run-1"):
        raise RuntimeError("boom")
    assert not lock.path.is_file()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_acquire_exactly_one_winner(tmp_path: Path) -> None:
    """Two threads acquiring simultaneously: exactly one succeeds."""
    lock = DeploymentLock(tmp_path / "deploy.lock")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def _attempt(name: str) -> None:
        barrier.wait()
        try:
            lock.acquire(holder=name)
            results.append(f"{name}:acquired")
        except DeploymentLockHeldError:
            results.append(f"{name}:blocked")

    t1 = threading.Thread(target=_attempt, args=("A",))
    t2 = threading.Thread(target=_attempt, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 2
    acquired = [r for r in results if r.endswith(":acquired")]
    blocked = [r for r in results if r.endswith(":blocked")]
    assert len(acquired) == 1
    assert len(blocked) == 1
