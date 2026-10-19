# tests/modpack_upgrade/test_publish_paths.py

"""Unit tests for ``publish._resolve_trusted_keys_path``.

Slice 11 code; previously exercised only via the end-to-end
``--verify-only`` path, which always hit the documented default. This
file closes the remaining branches:

  * missing ``upgrade.toml``
  * unreadable / malformed ``upgrade.toml``
  * missing / non-dict ``[registry]``
  * missing / empty / non-string ``trusted_keys_path``
  * absolute path returned as-is
  * relative path resolved against ``project_root``

The function has no side effects; every test calls it directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.modpack_upgrade.cli.commands import publish as publish_mod


def _write_upgrade_toml(config_dir: Path, body: str) -> None:
    """Write ``upgrade.toml`` under ``config_dir``."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "upgrade.toml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Default fallback
# ---------------------------------------------------------------------------


def test_default_when_upgrade_toml_missing(tmp_path: Path) -> None:
    """No upgrade.toml yields the default config.d/trusted_keys.toml."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    result = publish_mod._resolve_trusted_keys_path(config_dir, tmp_path)
    assert result == config_dir / "trusted_keys.toml"


def test_default_when_upgrade_toml_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError reading upgrade.toml yields the default."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, '[registry]\ntrusted_keys_path = "custom.toml"\n')
    toml = config_dir / "upgrade.toml"

    real_open = open

    def _fail(path: object, *args: object, **kwargs: object) -> object:
        if str(path) == str(toml):
            raise OSError("simulated read failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fail)
    result = publish_mod._resolve_trusted_keys_path(config_dir, tmp_path)
    assert result == config_dir / "trusted_keys.toml"


def test_default_when_upgrade_toml_malformed(tmp_path: Path) -> None:
    """Malformed TOML yields the default."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, "not = valid = toml\n")
    result = publish_mod._resolve_trusted_keys_path(config_dir, tmp_path)
    assert result == config_dir / "trusted_keys.toml"


def test_default_when_registry_section_missing(tmp_path: Path) -> None:
    """No [registry] section yields the default."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, '[publication]\nsigning_key_env = "X"\n')
    result = publish_mod._resolve_trusted_keys_path(config_dir, tmp_path)
    assert result == config_dir / "trusted_keys.toml"


def test_default_when_registry_section_not_a_table(tmp_path: Path) -> None:
    """A scalar [registry] yields the default."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, 'registry = "scalar"\n')
    result = publish_mod._resolve_trusted_keys_path(config_dir, tmp_path)
    assert result == config_dir / "trusted_keys.toml"


def test_default_when_trusted_keys_path_missing(tmp_path: Path) -> None:
    """[registry] present but no trusted_keys_path key yields the default."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, "[registry]\nother = 'value'\n")
    result = publish_mod._resolve_trusted_keys_path(config_dir, tmp_path)
    assert result == config_dir / "trusted_keys.toml"


def test_default_when_trusted_keys_path_empty(tmp_path: Path) -> None:
    """An empty-string trusted_keys_path yields the default."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, '[registry]\ntrusted_keys_path = ""\n')
    result = publish_mod._resolve_trusted_keys_path(config_dir, tmp_path)
    assert result == config_dir / "trusted_keys.toml"


def test_default_when_trusted_keys_path_non_string(tmp_path: Path) -> None:
    """A non-string trusted_keys_path yields the default."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, "[registry]\ntrusted_keys_path = 42\n")
    result = publish_mod._resolve_trusted_keys_path(config_dir, tmp_path)
    assert result == config_dir / "trusted_keys.toml"


# ---------------------------------------------------------------------------
# Configured path
# ---------------------------------------------------------------------------


def test_absolute_path_returned_as_is(tmp_path: Path) -> None:
    """An absolute trusted_keys_path is returned verbatim."""
    config_dir = tmp_path / "config.d"
    custom = tmp_path / "elsewhere" / "keys.toml"
    _write_upgrade_toml(config_dir, f'[registry]\ntrusted_keys_path = "{custom}"\n')
    result = publish_mod._resolve_trusted_keys_path(config_dir, tmp_path)
    assert result == custom


def test_relative_path_resolved_against_project_root(tmp_path: Path) -> None:
    """A relative trusted_keys_path resolves against project_root."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, '[registry]\ntrusted_keys_path = "sub/dir/keys.toml"\n')
    result = publish_mod._resolve_trusted_keys_path(config_dir, tmp_path)
    assert result == tmp_path / "sub" / "dir" / "keys.toml"
