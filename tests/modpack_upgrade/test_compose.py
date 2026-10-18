# tests/modpack_upgrade/test_compose.py

"""Unit tests for ``cli.commands._compose`` (Slice 8 pair 3).

The module under test is the single source of truth for compose-file
resolution and hashing across ``acknowledge``, ``materialize``, and
``update``. These tests exercise every branch of ``resolve_compose_path``
and every failure mode of ``compute_compose_sha256`` directly, so the
downstream callers only need delegation smoke tests.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from minecraft.modpack_upgrade.cli.commands import _compose
from minecraft.modpack_upgrade.cli.commands._compose import (
    compute_compose_sha256,
    resolve_compose_path,
)
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError


def _hex_of(path: Path) -> str:
    """Return the SHA-256 of a file's raw bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_config(config_dir: Path, body: str) -> None:
    """Write ``deploy_pack.toml`` under ``config_dir``."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "deploy_pack.toml").write_text(body, encoding="utf-8")


def _write_compose(project_root: Path, body: str = "services:\n  a:\n    image: x\n") -> Path:
    """Write a minimal valid compose file at the default location."""
    project_root.mkdir(parents=True, exist_ok=True)
    compose_path = project_root / "docker-compose.yml"
    compose_path.write_text(body, encoding="utf-8")
    return compose_path


# ---------------------------------------------------------------------------
# resolve_compose_path - default cases
# ---------------------------------------------------------------------------


def test_resolve_no_deploy_toml_returns_default(tmp_path: Path) -> None:
    """No deploy_pack.toml -> default docker-compose.yml at project root."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    assert resolve_compose_path(config_dir, tmp_path) == tmp_path / "docker-compose.yml"


def test_resolve_empty_deploy_toml_returns_default(tmp_path: Path) -> None:
    """A deploy_pack.toml without a [docker] table -> default."""
    config_dir = tmp_path / "config.d"
    _write_config(config_dir, '[instances.survival]\ncontainer = "mc-survival"\n')
    assert resolve_compose_path(config_dir, tmp_path) == tmp_path / "docker-compose.yml"


def test_resolve_docker_table_empty_returns_default(tmp_path: Path) -> None:
    """A [docker] table with no compose_file key -> default."""
    config_dir = tmp_path / "config.d"
    _write_config(config_dir, "[docker]\nrestart_wait_seconds = 30\n")
    assert resolve_compose_path(config_dir, tmp_path) == tmp_path / "docker-compose.yml"


def test_resolve_empty_string_returns_default(tmp_path: Path) -> None:
    """An empty-string compose_file is equivalent to absent -> default."""
    config_dir = tmp_path / "config.d"
    _write_config(config_dir, '[docker]\ncompose_file = ""\n')
    assert resolve_compose_path(config_dir, tmp_path) == tmp_path / "docker-compose.yml"


def test_resolve_non_string_returns_default(tmp_path: Path) -> None:
    """A non-string compose_file (e.g. int) -> default."""
    config_dir = tmp_path / "config.d"
    _write_config(config_dir, "[docker]\ncompose_file = 42\n")
    assert resolve_compose_path(config_dir, tmp_path) == tmp_path / "docker-compose.yml"


def test_resolve_malformed_toml_returns_default(tmp_path: Path) -> None:
    """A malformed deploy_pack.toml -> default (never raises)."""
    config_dir = tmp_path / "config.d"
    _write_config(config_dir, "not = valid = toml\n")
    assert resolve_compose_path(config_dir, tmp_path) == tmp_path / "docker-compose.yml"


def test_resolve_non_table_docker_returns_default(tmp_path: Path) -> None:
    """A [docker] key that is not a table -> default."""
    config_dir = tmp_path / "config.d"
    _write_config(config_dir, 'docker = "scalar"\n')
    assert resolve_compose_path(config_dir, tmp_path) == tmp_path / "docker-compose.yml"


# ---------------------------------------------------------------------------
# resolve_compose_path - override cases
# ---------------------------------------------------------------------------


def test_resolve_relative_override(tmp_path: Path) -> None:
    """A relative compose_file path resolves against project_root."""
    config_dir = tmp_path / "config.d"
    _write_config(config_dir, '[docker]\ncompose_file = "custom.yml"\n')
    assert resolve_compose_path(config_dir, tmp_path) == tmp_path / "custom.yml"


def test_resolve_relative_nested_override(tmp_path: Path) -> None:
    """A nested relative compose_file path resolves against project_root."""
    config_dir = tmp_path / "config.d"
    _write_config(config_dir, '[docker]\ncompose_file = "sub/dir/custom.yml"\n')
    assert resolve_compose_path(config_dir, tmp_path) == tmp_path / "sub" / "dir" / "custom.yml"


def test_resolve_absolute_override(tmp_path: Path) -> None:
    """An absolute compose_file path is used verbatim."""
    config_dir = tmp_path / "config.d"
    custom = tmp_path / "elsewhere" / "custom.yml"
    _write_config(config_dir, f'[docker]\ncompose_file = "{custom}"\n')
    assert resolve_compose_path(config_dir, tmp_path) == custom


# ---------------------------------------------------------------------------
# compute_compose_sha256 - success paths
# ---------------------------------------------------------------------------


def test_compute_sha256_default_location(tmp_path: Path) -> None:
    """A valid compose file at the default location hashes to its bytes."""
    compose_path = _write_compose(tmp_path)
    assert compute_compose_sha256(tmp_path, tmp_path) == _hex_of(compose_path)


def test_compute_sha256_relative_override(tmp_path: Path) -> None:
    """A compose file resolved from a relative override hashes correctly."""
    config_dir = tmp_path / "config.d"
    _write_config(config_dir, '[docker]\ncompose_file = "custom.yml"\n')
    compose_path = tmp_path / "custom.yml"
    compose_path.write_text("services:\n  b:\n    image: y\n", encoding="utf-8")
    assert compute_compose_sha256(config_dir, tmp_path) == _hex_of(compose_path)


def test_compute_sha256_absolute_override(tmp_path: Path) -> None:
    """A compose file resolved from an absolute override hashes correctly."""
    config_dir = tmp_path / "config.d"
    custom = tmp_path / "abs" / "compose.yml"
    custom.parent.mkdir(parents=True)
    custom.write_text("services:\n  c:\n    image: z\n", encoding="utf-8")
    _write_config(config_dir, f'[docker]\ncompose_file = "{custom}"\n')
    assert compute_compose_sha256(config_dir, tmp_path) == _hex_of(custom)


def test_compute_sha256_is_stable_across_calls(tmp_path: Path) -> None:
    """Repeated hashing of the same file yields the same digest."""
    _write_compose(tmp_path)
    a = compute_compose_sha256(tmp_path, tmp_path)
    b = compute_compose_sha256(tmp_path, tmp_path)
    assert a == b
    assert len(a) == 64


def test_compute_sha256_changes_when_bytes_change(tmp_path: Path) -> None:
    """Any change to the file's bytes changes the digest."""
    compose_path = _write_compose(tmp_path)
    before = compute_compose_sha256(tmp_path, tmp_path)
    compose_path.write_text("services:\n  a:\n    image: different\n", encoding="utf-8")
    after = compute_compose_sha256(tmp_path, tmp_path)
    assert before != after


# ---------------------------------------------------------------------------
# compute_compose_sha256 - failure paths
# ---------------------------------------------------------------------------


def test_compute_sha256_missing_file_raises(tmp_path: Path) -> None:
    """A missing compose file raises UpgradeCliError(3)."""
    with pytest.raises(UpgradeCliError) as ei:
        compute_compose_sha256(tmp_path, tmp_path)
    assert ei.value.exit_code == 3
    assert "not found" in str(ei.value)


def test_compute_sha256_parse_error_raises(tmp_path: Path) -> None:
    """A compose file that does not parse as YAML raises UpgradeCliError(3)."""
    (tmp_path / "docker-compose.yml").write_text(":::not yaml", encoding="utf-8")
    with pytest.raises(UpgradeCliError) as ei:
        compute_compose_sha256(tmp_path, tmp_path)
    assert ei.value.exit_code == 3
    assert "compose" in str(ei.value).lower()


def test_compute_sha256_read_error_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError during read_bytes raises UpgradeCliError(3).

    Distinct from missing-file: the file exists but cannot be read
    (permission denied, I/O error, etc.).
    """
    _write_compose(tmp_path)
    original_read_bytes = Path.read_bytes

    def _raise(self: Path) -> bytes:
        if self.name == "docker-compose.yml":
            raise OSError("simulated read failure")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _raise)
    with pytest.raises(UpgradeCliError) as ei:
        compute_compose_sha256(tmp_path, tmp_path)
    assert ei.value.exit_code == 3
    assert "could not read" in str(ei.value)


def test_compute_sha256_does_not_cache_negative_results(tmp_path: Path) -> None:
    """A missing file is re-checked each call; no cached failure."""
    with pytest.raises(UpgradeCliError):
        compute_compose_sha256(tmp_path, tmp_path)
    # Create the file; the next call must succeed.
    _write_compose(tmp_path)
    digest = compute_compose_sha256(tmp_path, tmp_path)
    assert len(digest) == 64


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_module_dunder_all() -> None:
    """__all__ exports exactly the two public helpers."""
    assert set(_compose.__all__) == {"compute_compose_sha256", "resolve_compose_path"}
