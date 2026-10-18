# tests/modpack_upgrade/test_inhouse.py

"""In-house filename list loading tests (Section 6.1, Section 9.5, A10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.inhouse import load_in_house_filenames


def _write_upgrade_toml(config_dir: Path, body: str) -> None:
    """Write upgrade.toml under ``config_dir``."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "upgrade.toml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_missing_upgrade_toml_returns_empty(tmp_path: Path) -> None:
    """No upgrade.toml is equivalent to no in-house mods."""
    assert load_in_house_filenames(tmp_path) == []


def test_missing_in_house_table_returns_empty(tmp_path: Path) -> None:
    """upgrade.toml without an [in-house] table returns []."""
    _write_upgrade_toml(tmp_path, '[resolver]\nrelease_policy = "release_then_beta"\n')
    assert load_in_house_filenames(tmp_path) == []


def test_empty_filenames_list_returns_empty(tmp_path: Path) -> None:
    """An empty filenames list returns []."""
    _write_upgrade_toml(tmp_path, "[in-house]\nfilenames = []\n")
    assert load_in_house_filenames(tmp_path) == []


def test_filenames_returned_sorted(tmp_path: Path) -> None:
    """The returned list is sorted lexicographically."""
    _write_upgrade_toml(
        tmp_path,
        '[in-house]\nfilenames = ["zeta.jar", "alpha.jar", "mu.jar"]\n',
    )
    assert load_in_house_filenames(tmp_path) == ["alpha.jar", "mu.jar", "zeta.jar"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_malformed_toml_raises(tmp_path: Path) -> None:
    """Malformed upgrade.toml raises SchemaError."""
    _write_upgrade_toml(tmp_path, "not = valid = toml\n")
    with pytest.raises(SchemaError):
        load_in_house_filenames(tmp_path)


def test_non_dict_section_raises(tmp_path: Path) -> None:
    """A non-table [in-house] raises SchemaError."""
    _write_upgrade_toml(tmp_path, 'in-house = "scalar"\n')
    with pytest.raises(SchemaError, match="must be a table"):
        load_in_house_filenames(tmp_path)


def test_missing_filenames_raises(tmp_path: Path) -> None:
    """[in-house] present but no filenames key raises SchemaError."""
    _write_upgrade_toml(tmp_path, "[in-house]\nother_key = true\n")
    with pytest.raises(SchemaError, match="filenames is missing"):
        load_in_house_filenames(tmp_path)


def test_filenames_not_list_raises(tmp_path: Path) -> None:
    """A non-list filenames value raises SchemaError."""
    _write_upgrade_toml(tmp_path, '[in-house]\nfilenames = "not-a-list"\n')
    with pytest.raises(SchemaError, match="must be an array of strings"):
        load_in_house_filenames(tmp_path)


def test_filenames_with_non_string_raises(tmp_path: Path) -> None:
    """A list containing non-strings raises SchemaError."""
    _write_upgrade_toml(tmp_path, "[in-house]\nfilenames = [1, 2, 3]\n")
    with pytest.raises(SchemaError, match="must be an array of strings"):
        load_in_house_filenames(tmp_path)
