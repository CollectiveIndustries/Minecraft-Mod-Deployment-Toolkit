# tests/modpack_upgrade/test_usage.py

"""Unit tests for ``modpack_upgrade.usage`` -- budget tracking and cap enforcement."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import tomli_w

from minecraft.modpack_upgrade import usage
from minecraft.modpack_upgrade.usage import (
    UsageCapExceeded,
    UsageConfig,
    UsageLogReadError,
    UsageTracker,
    load_usage_config,
)


def _config(tmp_path: Path, **overrides: object) -> UsageConfig:
    """Return a UsageConfig with sensible defaults for tests."""
    base: dict[str, object] = {
        "log_path": tmp_path / "usage.jsonl",
        "window_hours": 24,
        "max_api_requests": None,
        "max_downloaded_bytes": None,
    }
    base.update(overrides)
    return UsageConfig(**base)  # type: ignore[arg-type]


def _write_upgrade_toml(config_dir: Path, body: dict) -> None:
    """Write ``upgrade.toml`` under ``config_dir`` as TOML."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "upgrade.toml").write_bytes(tomli_w.dumps(body).encode("utf-8"))


# ---------------------------------------------------------------------------
# UsageConfig.enabled
# ---------------------------------------------------------------------------


def test_config_disabled_when_no_caps(tmp_path: Path) -> None:
    """A config with no caps is not enabled."""
    assert _config(tmp_path).enabled is False


def test_config_enabled_with_api_cap_only(tmp_path: Path) -> None:
    """An API cap alone enables tracking."""
    assert _config(tmp_path, max_api_requests=10).enabled is True


def test_config_enabled_with_byte_cap_only(tmp_path: Path) -> None:
    """A byte cap alone enables tracking."""
    assert _config(tmp_path, max_downloaded_bytes=1024).enabled is True


# ---------------------------------------------------------------------------
# load_usage_config
# ---------------------------------------------------------------------------


def test_load_no_upgrade_toml_returns_none(tmp_path: Path) -> None:
    """A missing upgrade.toml returns None."""
    assert load_usage_config(tmp_path, tmp_path) is None


def test_load_no_budget_section_returns_none(tmp_path: Path) -> None:
    """An upgrade.toml without [budget] returns None."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, {"resolver": {"release_policy": "x"}})
    assert load_usage_config(config_dir, tmp_path) is None


def test_load_budget_no_caps_returns_none(tmp_path: Path) -> None:
    """A [budget] with no caps returns None even if log_path is set."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, {"budget": {"usage_log_path": "var/usage.jsonl"}})
    assert load_usage_config(config_dir, tmp_path) is None


def test_load_budget_api_cap_only(tmp_path: Path) -> None:
    """An API cap alone produces a config with only that cap."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, {"budget": {"max_api_requests": 5000}})
    cfg = load_usage_config(config_dir, tmp_path)
    assert cfg is not None
    assert cfg.max_api_requests == 5000
    assert cfg.max_downloaded_bytes is None
    assert cfg.log_path == tmp_path / "var" / "usage.jsonl"


def test_load_budget_byte_cap_only(tmp_path: Path) -> None:
    """A byte cap alone produces a config with only that cap."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, {"budget": {"max_downloaded_bytes": 10_000_000}})
    cfg = load_usage_config(config_dir, tmp_path)
    assert cfg is not None
    assert cfg.max_api_requests is None
    assert cfg.max_downloaded_bytes == 10_000_000


def test_load_budget_both_caps(tmp_path: Path) -> None:
    """Both caps produces a config with both."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(
        config_dir,
        {
            "budget": {
                "max_api_requests": 5000,
                "max_downloaded_bytes": 10_000_000_000,
            }
        },
    )
    cfg = load_usage_config(config_dir, tmp_path)
    assert cfg is not None
    assert cfg.max_api_requests == 5000
    assert cfg.max_downloaded_bytes == 10_000_000_000


def test_load_budget_custom_log_path_relative(tmp_path: Path) -> None:
    """A relative usage_log_path resolves against project_root."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(
        config_dir,
        {"budget": {"max_api_requests": 10, "usage_log_path": "custom/u.jsonl"}},
    )
    cfg = load_usage_config(config_dir, tmp_path)
    assert cfg is not None
    assert cfg.log_path == tmp_path / "custom" / "u.jsonl"


def test_load_budget_custom_log_path_absolute(tmp_path: Path) -> None:
    """An absolute usage_log_path is used verbatim."""
    config_dir = tmp_path / "config.d"
    custom = tmp_path / "elsewhere" / "u.jsonl"
    _write_upgrade_toml(
        config_dir,
        {"budget": {"max_api_requests": 10, "usage_log_path": str(custom)}},
    )
    cfg = load_usage_config(config_dir, tmp_path)
    assert cfg is not None
    assert cfg.log_path == custom


def test_load_budget_custom_window(tmp_path: Path) -> None:
    """usage_window_hours is honoured when valid."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(
        config_dir,
        {"budget": {"max_api_requests": 10, "usage_window_hours": 1}},
    )
    cfg = load_usage_config(config_dir, tmp_path)
    assert cfg is not None
    assert cfg.window_hours == 1


def test_load_budget_invalid_window_defaults_to_24(tmp_path: Path) -> None:
    """A non-positive window falls back to 24."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(
        config_dir,
        {"budget": {"max_api_requests": 10, "usage_window_hours": 0}},
    )
    cfg = load_usage_config(config_dir, tmp_path)
    assert cfg is not None
    assert cfg.window_hours == 24


def test_load_budget_negative_cap_returns_none(tmp_path: Path) -> None:
    """A negative max_api_requests drops the whole config."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, {"budget": {"max_api_requests": -1}})
    assert load_usage_config(config_dir, tmp_path) is None


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def test_record_api_call_writes_line(tmp_path: Path) -> None:
    """record_api_call appends one JSON line."""
    tracker = UsageTracker(_config(tmp_path))
    tracker.record_api_call(provider="modrinth", endpoint="/version/abc")
    assert tracker.config.log_path.is_file()
    lines = tracker.config.log_path.read_text().splitlines()
    assert len(lines) == 1


def test_record_download_writes_bytes(tmp_path: Path) -> None:
    """record_download appends a download record with the byte count."""
    tracker = UsageTracker(_config(tmp_path))
    tracker.record_download(provider="cdn", url="https://x/y.jar", bytes_count=1024)
    content = tracker.config.log_path.read_text()
    assert '"bytes": 1024' in content


def test_record_includes_run_id_from_context(tmp_path: Path) -> None:
    """Records written inside run_context carry run_id."""
    tracker = UsageTracker(_config(tmp_path))
    with usage.run_context("run-42"):
        tracker.record_api_call(provider="modrinth", endpoint="/x")
    content = tracker.config.log_path.read_text()
    assert '"run_id": "run-42"' in content


def test_record_without_context_has_no_run_id(tmp_path: Path) -> None:
    """Records written outside run_context do not carry run_id."""
    tracker = UsageTracker(_config(tmp_path))
    tracker.record_api_call(provider="modrinth", endpoint="/x")
    content = tracker.config.log_path.read_text()
    assert '"run_id"' not in content


def test_record_creates_parent_directory(tmp_path: Path) -> None:
    """Recording creates the log path's parent directories."""
    log_path = tmp_path / "deep" / "nested" / "usage.jsonl"
    tracker = UsageTracker(_config(tmp_path, log_path=log_path))
    tracker.record_api_call(provider="x", endpoint="/y")
    assert log_path.is_file()


# ---------------------------------------------------------------------------
# Cap enforcement
# ---------------------------------------------------------------------------


def test_check_api_cap_below_cap_passes(tmp_path: Path) -> None:
    """API usage below cap does not raise."""
    tracker = UsageTracker(_config(tmp_path, max_api_requests=5))
    tracker.record_api_call(provider="x", endpoint="/y")
    tracker.check_api_cap()


def test_check_api_cap_at_cap_raises(tmp_path: Path) -> None:
    """API usage at cap raises UsageCapExceeded."""
    tracker = UsageTracker(_config(tmp_path, max_api_requests=2))
    for _ in range(2):
        tracker.record_api_call(provider="x", endpoint="/y")
    with pytest.raises(UsageCapExceeded) as ei:
        tracker.check_api_cap()
    assert ei.value.kind == "api_request"
    assert ei.value.used == 2
    assert ei.value.cap == 2


def test_check_api_cap_no_cap_passes(tmp_path: Path) -> None:
    """No configured cap means check is a no-op."""
    tracker = UsageTracker(_config(tmp_path, max_api_requests=None))
    for _ in range(100):
        tracker.record_api_call(provider="x", endpoint="/y")
    tracker.check_api_cap()


def test_check_byte_cap_at_cap_raises(tmp_path: Path) -> None:
    """Byte usage at cap raises UsageCapExceeded."""
    tracker = UsageTracker(_config(tmp_path, max_downloaded_bytes=1024))
    tracker.record_download(provider="x", url="u", bytes_count=1024)
    with pytest.raises(UsageCapExceeded) as ei:
        tracker.check_byte_cap()
    assert ei.value.kind == "downloaded_bytes"
    assert ei.value.used == 1024


def test_check_byte_cap_below_cap_passes(tmp_path: Path) -> None:
    """Byte usage below cap does not raise."""
    tracker = UsageTracker(_config(tmp_path, max_downloaded_bytes=2048))
    tracker.record_download(provider="x", url="u", bytes_count=1024)
    tracker.check_byte_cap()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


def _write_old_record(path: Path, ts: str) -> None:
    """Append a record with a specific timestamp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"timestamp_utc": ts, "kind": "api", "provider": "x", "endpoint": "/y"},
                sort_keys=True,
            )
            + "\n"
        )


def test_old_records_outside_window_ignored(tmp_path: Path) -> None:
    """Records older than the window do not count toward usage."""
    log_path = tmp_path / "usage.jsonl"
    tracker = UsageTracker(_config(tmp_path, log_path=log_path, window_hours=1, max_api_requests=10))
    old_ts = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _write_old_record(log_path, old_ts)
    assert tracker.api_used() == 0


def test_fresh_records_inside_window_counted(tmp_path: Path) -> None:
    """Records newer than the window count toward usage."""
    tracker = UsageTracker(_config(tmp_path, window_hours=24, max_api_requests=10))
    tracker.record_api_call(provider="x", endpoint="/y")
    assert tracker.api_used() == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_unreadable_log_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError on read raises UsageLogReadError."""
    tracker = UsageTracker(_config(tmp_path, max_api_requests=10))
    tracker.record_api_call(provider="x", endpoint="/y")
    log_path = tracker.config.log_path

    real_open = open

    def _fail(path: object, *args: object, **kwargs: object) -> object:
        if str(path) == str(log_path):
            raise OSError("simulated")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fail)
    with pytest.raises(UsageLogReadError):
        tracker.check_api_cap()


def test_malformed_line_skipped(tmp_path: Path) -> None:
    """A malformed line in the log is skipped, not fatal."""
    tracker = UsageTracker(_config(tmp_path, max_api_requests=10))
    log_path = tracker.config.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("not json\n")
    tracker.record_api_call(provider="x", endpoint="/y")
    assert tracker.api_used() == 1


def test_missing_file_returns_zero_usage(tmp_path: Path) -> None:
    """A missing log file yields zero usage on first check."""
    tracker = UsageTracker(_config(tmp_path, max_api_requests=10))
    assert tracker.api_used() == 0
    assert tracker.bytes_used() == 0
