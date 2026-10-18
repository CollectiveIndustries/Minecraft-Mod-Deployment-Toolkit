# tests/deploy_pack/test_daemon_idempotency.py

"""SQLite idempotency ledger tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.deploy_pack.daemon.errors import IdempotencyConflictError
from minecraft.deploy_pack.daemon.idempotency import SQLiteIdempotency


def _make_ledger(tmp_path: Path) -> SQLiteIdempotency:
    """Return a fresh ledger in ``tmp_path``."""
    return SQLiteIdempotency(tmp_path / "ledger.sqlite", ttl_hours=24)


def test_lookup_missing(tmp_path: Path):
    """An unknown key returns None."""
    ledger = _make_ledger(tmp_path)
    assert ledger.lookup("k1", "verify", "hash") is None


def test_store_and_lookup_round_trip(tmp_path: Path):
    """A stored response is returned on lookup."""
    ledger = _make_ledger(tmp_path)
    ledger.store("k1", "verify", "hash", 200, {"status": "ok"})
    assert ledger.lookup("k1", "verify", "hash") == (200, {"status": "ok"})


def test_endpoint_mismatch_conflicts(tmp_path: Path):
    """The key is bound to its endpoint."""
    ledger = _make_ledger(tmp_path)
    ledger.store("k1", "verify", "hash", 200, {"status": "ok"})
    with pytest.raises(IdempotencyConflictError):
        ledger.lookup("k1", "publish", "hash")


def test_hash_mismatch_conflicts(tmp_path: Path):
    """The key is bound to its body hash."""
    ledger = _make_ledger(tmp_path)
    ledger.store("k1", "verify", "hash-a", 200, {"status": "ok"})
    with pytest.raises(IdempotencyConflictError):
        ledger.lookup("k1", "verify", "hash-b")


def test_different_keys_independent(tmp_path: Path):
    """Two keys resolve independently."""
    ledger = _make_ledger(tmp_path)
    ledger.store("k1", "verify", "hash-a", 200, {"status": "ok"})
    ledger.store("k2", "verify", "hash-b", 200, {"status": "blocked"})
    assert ledger.lookup("k1", "verify", "hash-a") == (200, {"status": "ok"})
    assert ledger.lookup("k2", "verify", "hash-b") == (200, {"status": "blocked"})


def test_zero_ttl_prunes_immediately(tmp_path: Path):
    """With ttl_hours=0 every prior row is pruned on the next write."""
    ledger = SQLiteIdempotency(tmp_path / "ledger.sqlite", ttl_hours=0)
    ledger.store("k1", "verify", "hash-a", 200, {"status": "ok"})
    ledger.store("k2", "verify", "hash-b", 200, {"status": "blocked"})
    assert ledger.lookup("k1", "verify", "hash-a") is None
    assert ledger.lookup("k2", "verify", "hash-b") == (200, {"status": "blocked"})


def test_creates_parent_dir(tmp_path: Path):
    """A nested db path is created if missing."""
    nested = tmp_path / "a" / "b" / "c" / "ledger.sqlite"
    ledger = SQLiteIdempotency(nested, ttl_hours=24)
    ledger.store("k1", "verify", "hash", 200, {"status": "ok"})
    assert nested.is_file()
