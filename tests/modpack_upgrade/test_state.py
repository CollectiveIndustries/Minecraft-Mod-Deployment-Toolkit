# tests/modpack_upgrade/test_state.py

"""RunState serialization and validation tests (Resolution_Report Section 6.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.state import (
    STATUS_ACKNOWLEDGED,
    STATUS_AWAITING_ACKNOWLEDGMENT,
    STATUS_FAILED,
    STATUS_MATERIALIZED,
    STATUS_RESOLVED,
    RunState,
    load_state,
    save_state,
)


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _valid_state(**overrides: object) -> RunState:
    """Build a valid RunState with sensible defaults."""
    base: dict[str, object] = {
        "run_id": "abc",
        "status": STATUS_RESOLVED,
        "release_mode": "update",
        "paused_at_utc": "2026-01-01T00:00:00Z",
        "report_sha256": _hex("a"),
        "resolution_inputs_sha256": _hex("b"),
        "manifest_path": "/srv/m/sync/modpack.manifest.toml",
        "effective_compose_sha256": _hex("c"),
    }
    base.update(overrides)
    return RunState(**base)


def _write_state(tmp_path: Path, body: str) -> Path:
    """Write ``body`` as state.toml and return the path."""
    p = tmp_path / "state.toml"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_update_mode(tmp_path: Path) -> None:
    """Update-mode state round-trips with the compose hash preserved."""
    state = _valid_state()
    save_state(tmp_path / "state.toml", state)
    assert load_state(tmp_path / "state.toml") == state


def test_round_trip_upgrade_mode_omits_compose_hash(tmp_path: Path) -> None:
    """Upgrade-mode state omits the compose hash field."""
    state = _valid_state(release_mode="upgrade", effective_compose_sha256=None)
    save_state(tmp_path / "state.toml", state)
    loaded = load_state(tmp_path / "state.toml")
    assert loaded.release_mode == "upgrade"
    assert loaded.effective_compose_sha256 is None


def test_round_trip_with_reason(tmp_path: Path) -> None:
    """An optional ``reason`` field survives the round trip."""
    state = _valid_state(status=STATUS_FAILED, reason="cap_exhausted")
    save_state(tmp_path / "state.toml", state)
    assert load_state(tmp_path / "state.toml").reason == "cap_exhausted"


@pytest.mark.parametrize(
    "status",
    [
        STATUS_RESOLVED,
        STATUS_AWAITING_ACKNOWLEDGMENT,
        STATUS_ACKNOWLEDGED,
        STATUS_MATERIALIZED,
        STATUS_FAILED,
    ],
)
def test_round_trip_all_statuses(tmp_path: Path, status: str) -> None:
    """Every documented status value round-trips cleanly."""
    state = _valid_state(status=status)
    save_state(tmp_path / "state.toml", state)
    assert load_state(tmp_path / "state.toml").status == status


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_missing_file_raises(tmp_path: Path) -> None:
    """A missing state.toml raises SchemaError."""
    with pytest.raises(SchemaError):
        load_state(tmp_path / "nope.toml")


def test_malformed_toml_raises(tmp_path: Path) -> None:
    """Malformed TOML raises SchemaError."""
    p = _write_state(tmp_path, "not = valid = toml\n")
    with pytest.raises(SchemaError):
        load_state(p)


def test_missing_required_field_raises(tmp_path: Path) -> None:
    """Any missing required field raises SchemaError."""
    p = _write_state(tmp_path, 'run_id = "x"\n')
    with pytest.raises(SchemaError, match="missing required field"):
        load_state(p)


def test_invalid_status_raises(tmp_path: Path) -> None:
    """An unknown status value raises SchemaError."""
    p = _write_state(
        tmp_path,
        f'run_id = "x"\n'
        f'status = "bogus"\n'
        f'release_mode = "update"\n'
        f'paused_at_utc = "t"\n'
        f'report_sha256 = "{_hex("a")}"\n'
        f'resolution_inputs_sha256 = "{_hex("b")}"\n'
        f'manifest_path = "/m"\n'
        f'effective_compose_sha256 = "{_hex("c")}"\n',
    )
    with pytest.raises(SchemaError, match="invalid status"):
        load_state(p)


def test_invalid_release_mode_raises(tmp_path: Path) -> None:
    """An unknown release_mode raises SchemaError."""
    p = _write_state(
        tmp_path,
        f'run_id = "x"\n'
        f'status = "resolved"\n'
        f'release_mode = "bogus"\n'
        f'paused_at_utc = "t"\n'
        f'report_sha256 = "{_hex("a")}"\n'
        f'resolution_inputs_sha256 = "{_hex("b")}"\n'
        f'manifest_path = "/m"\n',
    )
    with pytest.raises(SchemaError, match="invalid release_mode"):
        load_state(p)


def test_update_mode_without_compose_hash_raises(tmp_path: Path) -> None:
    """release_mode='update' requires effective_compose_sha256."""
    p = _write_state(
        tmp_path,
        f'run_id = "x"\n'
        f'status = "resolved"\n'
        f'release_mode = "update"\n'
        f'paused_at_utc = "t"\n'
        f'report_sha256 = "{_hex("a")}"\n'
        f'resolution_inputs_sha256 = "{_hex("b")}"\n'
        f'manifest_path = "/m"\n',
    )
    with pytest.raises(SchemaError, match="requires effective_compose_sha256"):
        load_state(p)


def test_save_creates_parent_directory(tmp_path: Path) -> None:
    """save_state creates missing parent directories."""
    nested = tmp_path / "a" / "b" / "state.toml"
    save_state(nested, _valid_state())
    assert nested.is_file()
