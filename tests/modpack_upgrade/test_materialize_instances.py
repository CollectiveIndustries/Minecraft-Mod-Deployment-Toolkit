# tests/modpack_upgrade/test_materialize_instances.py

"""Additional unit tests for ``materialize._load_instances``.

Complements ``test_materialize.py`` (which covers happy-path parsing
and per-field rejection cases) with the top-level failure modes:

  * missing deploy_pack.toml
  * malformed TOML
  * no [instances] table
  * empty [instances] table
  * [instances.X] is not a table

All five raise ``UpgradeCliError(3)`` with a specific message; the
operator needs that message to be actionable when they forget the
section, mistype the filename, or write a scalar where a table is
expected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.modpack_upgrade.cli.commands import materialize as materialize_mod
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError


def _write(config_dir: Path, body: str) -> None:
    """Write ``deploy_pack.toml`` under ``config_dir``."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "deploy_pack.toml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Top-level structure failures
# ---------------------------------------------------------------------------


def test_missing_deploy_pack_toml_raises(tmp_path: Path) -> None:
    """No deploy_pack.toml at all yields UpgradeCliError(3)."""
    with pytest.raises(UpgradeCliError) as ei:
        materialize_mod._load_instances(tmp_path)
    assert ei.value.exit_code == 3
    assert "deploy_pack.toml not found" in str(ei.value)


def test_malformed_toml_raises(tmp_path: Path) -> None:
    """Malformed TOML yields UpgradeCliError(3)."""
    _write(tmp_path, "not = valid = toml\n")
    with pytest.raises(UpgradeCliError) as ei:
        materialize_mod._load_instances(tmp_path)
    assert ei.value.exit_code == 3
    assert "could not parse" in str(ei.value)


def test_no_instances_section_raises(tmp_path: Path) -> None:
    """No [instances] table yields UpgradeCliError(3)."""
    _write(tmp_path, "[docker]\nrestart_wait_seconds = 30\n")
    with pytest.raises(UpgradeCliError) as ei:
        materialize_mod._load_instances(tmp_path)
    assert ei.value.exit_code == 3
    assert "no [instances] table" in str(ei.value)


def test_empty_instances_table_raises(tmp_path: Path) -> None:
    """An empty [instances] table yields UpgradeCliError(3)."""
    _write(tmp_path, "[instances]\n")
    with pytest.raises(UpgradeCliError) as ei:
        materialize_mod._load_instances(tmp_path)
    assert ei.value.exit_code == 3
    assert "no [instances] table" in str(ei.value)


def test_instance_block_not_a_table_raises(tmp_path: Path) -> None:
    """[instances.X] as a scalar yields UpgradeCliError(3)."""
    _write(tmp_path, 'instances = {survival = "not-a-table"}\n')
    with pytest.raises(UpgradeCliError) as ei:
        materialize_mod._load_instances(tmp_path)
    assert ei.value.exit_code == 3
    assert "must be a table" in str(ei.value)
