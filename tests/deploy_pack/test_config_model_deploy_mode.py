# tests/deploy_pack/test_config_model_deploy_mode.py

"""Deploy-mode resolution tests (Slice 17, config_model.py).

Two layers:

  1. Direct unit tests for ``_resolve_deploy_mode`` - every branch of the
     CLI > .env > "prod" precedence chain, plus the malformed-value raise
     paths.
  2. Integration tests through ``load_deployment_config`` that pin the
     mode-selection contracts: ``[docker].compose_file_<mode>`` picks the
     compose file, ``[instances.X].mode`` filters the partition, and
     ``DeploymentConfig.filtered_instances`` records why a name was
     excluded.

The risk report flagged ``_resolve_deploy_mode`` at 37.5% coverage
before this file existed; the branch table below is deliberately
exhaustive so a regression in the precedence chain fails loudly.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from minecraft.deploy_pack.config_model import (
    _DEFAULT_DEPLOY_MODE,
    _VALID_DEPLOY_MODES,
    _resolve_deploy_mode,
    load_deployment_config,
)
from minecraft.deploy_pack.errors import ConfigError

_LOGGER = logging.getLogger("test_deploy_mode")


# ===========================================================================
# Section 1  _resolve_deploy_mode: CLI > .env > default
# ===========================================================================


def test_constants_are_the_spec_values() -> None:
    """The mode set and default are the two strings Slice 17 defines."""
    assert frozenset({"prod", "dev"}) == _VALID_DEPLOY_MODES
    assert _DEFAULT_DEPLOY_MODE == "prod"


def test_cli_override_prod_returns_prod() -> None:
    """A valid CLI override for prod is returned verbatim."""
    assert _resolve_deploy_mode({}, "prod", _LOGGER) == "prod"


def test_cli_override_dev_returns_dev() -> None:
    """A valid CLI override for dev is returned verbatim."""
    assert _resolve_deploy_mode({}, "dev", _LOGGER) == "dev"


def test_cli_override_wins_over_env() -> None:
    """CLI > .env: a valid CLI override beats a valid env value."""
    raw = {"deploy_mode": "dev"}
    assert _resolve_deploy_mode(raw, "prod", _LOGGER) == "prod"


def test_invalid_cli_override_raises() -> None:
    """A CLI value outside {prod, dev} is a ConfigError."""
    with pytest.raises(ConfigError, match="--deploy-mode"):
        _resolve_deploy_mode({}, "bogus", _LOGGER)


def test_env_prod_returns_prod_when_cli_absent() -> None:
    """With no CLI override, a valid .env value is used."""
    assert _resolve_deploy_mode({"deploy_mode": "prod"}, None, _LOGGER) == "prod"


def test_env_dev_returns_dev_when_cli_absent() -> None:
    """With no CLI override, a valid dev .env value is used."""
    assert _resolve_deploy_mode({"deploy_mode": "dev"}, None, _LOGGER) == "dev"


def test_invalid_env_value_raises() -> None:
    """An .env value outside {prod, dev} is a ConfigError."""
    with pytest.raises(ConfigError, match="deploy_mode"):
        _resolve_deploy_mode({"deploy_mode": "bogus"}, None, _LOGGER)


def test_non_string_env_value_raises() -> None:
    """An .env value that is not a string is a ConfigError.

    The file loader always produces strings; this guards the type check
    against a well-meaning refactor that drops it.
    """
    with pytest.raises(ConfigError, match="deploy_mode"):
        _resolve_deploy_mode({"deploy_mode": 42}, None, _LOGGER)


def test_neither_cli_nor_env_defaults_to_prod() -> None:
    """No CLI override and no .env key yields the default."""
    assert _resolve_deploy_mode({}, None, _LOGGER) == "prod"


def test_empty_raw_defaults_to_prod() -> None:
    """An empty raw dict is the same as a missing key."""
    assert _resolve_deploy_mode({}, None, _LOGGER) == _DEFAULT_DEPLOY_MODE


# ===========================================================================
# Section 2  End-to-end through load_deployment_config
# ===========================================================================


def _prod_compose() -> str:
    """Return a compose file declaring the prod-side services."""
    return (
        "services:\n"
        "  mc-survival:\n"
        "    container_name: mc-survival\n"
        '    healthcheck:\n      test: ["CMD", "true"]\n'
        "    volumes:\n"
        "      - ./data/survival:/data\n"
        "      - ./shared/mods:/data/mods\n"
        "  nginx:\n"
        "    container_name: nginx\n"
        "    volumes:\n"
        "      - ./www:/usr/share/nginx/html\n"
    )


def _dev_compose() -> str:
    """Return a compose file declaring the dev-side services."""
    return (
        "services:\n"
        "  mc-latest:\n"
        "    container_name: mc-latest\n"
        '    healthcheck:\n      test: ["CMD", "true"]\n'
        "    volumes:\n"
        "      - ./latest-data:/data\n"
        "      - ./dev-mods:/data/mods\n"
        "  mc-artifact-store:\n"
        "    container_name: mc-artifact-store\n"
        "    volumes:\n"
        "      - ./sync/downloads:/usr/share/nginx/html:ro\n"
    )


_DEFAULT_DOCKER_BLOCK = 'compose_file_prod = "./docker-compose.yml"\ncompose_file_dev  = "./docker-compose.dev.yml"'

_DEFAULT_INSTANCES_BLOCK = '[instances.survival]\ncontainer = "mc-survival"\nmode = "prod"\n\n[instances.latest]\ncontainer = "mc-latest"\nmode = "dev"\n'


def _write_repo(
    tmp_path: Path,
    *,
    docker_block: str | None = None,
    instances_block: str | None = None,
) -> Path:
    """Write a minimal repo with both compose files and mode-tagged instances."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (tmp_path / "docker-compose.yml").write_text(_prod_compose(), encoding="utf-8")
    (tmp_path / "docker-compose.dev.yml").write_text(_dev_compose(), encoding="utf-8")
    docker = docker_block if docker_block is not None else _DEFAULT_DOCKER_BLOCK
    instances = instances_block if instances_block is not None else _DEFAULT_INSTANCES_BLOCK
    toml = (
        'instance_discovery = "explicit"\n'
        'sync_root    = "./sync"\n'
        'modpack_dir  = "./sync/downloads"\n'
        'output_filename   = "minecraft_client_{date}.zip"\n'
        'download_base_url = "http://minecraft/downloads"\n'
        "\n[sync_mapping]\n"
        'config = "config"\n'
        'kubejs = "kubejs"\n'
        "\n[docker]\n"
        f"{docker}\n"
        "\n"
        f"{instances}\n"
    )
    (config_dir / "deploy_pack.toml").write_text(toml, encoding="utf-8")
    return config_dir


def _write_env(config_dir: Path, body: str) -> None:
    """Write ``config.d/.env`` with ``body``."""
    (config_dir / ".env").write_text(body, encoding="utf-8")


def test_load_defaults_to_prod_mode(tmp_path: Path) -> None:
    """No CLI override and no .env -> prod mode, prod compose file."""
    cfg = load_deployment_config(_write_repo(tmp_path))
    assert cfg.deploy_mode == "prod"
    assert cfg.docker.compose_file.name == "docker-compose.yml"


def test_load_env_sets_dev_mode(tmp_path: Path) -> None:
    """.env deploy_mode=dev selects the dev compose file."""
    config_dir = _write_repo(tmp_path)
    _write_env(config_dir, "deploy_mode=dev\n")
    cfg = load_deployment_config(config_dir)
    assert cfg.deploy_mode == "dev"
    assert cfg.docker.compose_file.name == "docker-compose.dev.yml"


def test_load_cli_override_dev_selects_dev_compose(tmp_path: Path) -> None:
    """CLI --deploy-mode dev selects the dev compose file."""
    cfg = load_deployment_config(_write_repo(tmp_path), deploy_mode_override="dev")
    assert cfg.deploy_mode == "dev"
    assert cfg.docker.compose_file.name == "docker-compose.dev.yml"


def test_load_cli_override_wins_over_env(tmp_path: Path) -> None:
    """CLI dev + .env prod -> CLI wins."""
    config_dir = _write_repo(tmp_path)
    _write_env(config_dir, "deploy_mode=prod\n")
    cfg = load_deployment_config(config_dir, deploy_mode_override="dev")
    assert cfg.deploy_mode == "dev"
    assert cfg.docker.compose_file.name == "docker-compose.dev.yml"


def test_load_dev_mode_filters_to_dev_instances(tmp_path: Path) -> None:
    """Dev mode activates only the dev instances."""
    cfg = load_deployment_config(_write_repo(tmp_path), deploy_mode_override="dev")
    assert cfg.partition == ["latest"]
    assert cfg.filtered_instances == {"survival": "prod"}


def test_load_prod_mode_filters_to_prod_instances(tmp_path: Path) -> None:
    """Prod mode activates only the prod instances."""
    cfg = load_deployment_config(_write_repo(tmp_path))
    assert cfg.partition == ["survival"]
    assert cfg.filtered_instances == {"latest": "dev"}


def test_load_dev_mode_with_no_dev_instances_yields_empty_partition(tmp_path: Path) -> None:
    """Dev mode against a prod-only instance set has an empty partition."""
    config_dir = _write_repo(
        tmp_path,
        instances_block='[instances.survival]\ncontainer = "mc-survival"\nmode = "prod"\n',
    )
    cfg = load_deployment_config(config_dir, deploy_mode_override="dev")
    assert cfg.partition == []
    assert cfg.filtered_instances == {"survival": "prod"}


def test_load_instance_without_mode_defaults_to_prod(tmp_path: Path) -> None:
    """An instance with no mode field is prod."""
    config_dir = _write_repo(
        tmp_path,
        instances_block='[instances.survival]\ncontainer = "mc-survival"\n',
    )
    cfg = load_deployment_config(config_dir)
    assert cfg.partition == ["survival"]
    assert cfg.filtered_instances == {}


def test_load_env_deploy_mode_is_case_sensitive(tmp_path: Path) -> None:
    """.env keys are case-sensitive; DEPLOY_MODE does not set deploy_mode."""
    config_dir = _write_repo(tmp_path)
    _write_env(config_dir, "DEPLOY_MODE=dev\n")
    cfg = load_deployment_config(config_dir)
    assert cfg.deploy_mode == "prod"


def test_load_legacy_compose_file_fallback_when_mode_key_missing(tmp_path: Path) -> None:
    """A host that has not migrated falls back to legacy compose_file."""
    config_dir = _write_repo(tmp_path, docker_block='compose_file = "./docker-compose.yml"')
    cfg = load_deployment_config(config_dir, deploy_mode_override="dev")
    assert cfg.deploy_mode == "dev"
    # Legacy fallback wins: the prod compose file is loaded even in dev mode.
    assert cfg.docker.compose_file.name == "docker-compose.yml"


def test_load_invalid_env_mode_raises(tmp_path: Path) -> None:
    """An invalid .env mode value is a ConfigError."""
    config_dir = _write_repo(tmp_path)
    _write_env(config_dir, "deploy_mode=bogus\n")
    with pytest.raises(ConfigError, match="deploy_mode"):
        load_deployment_config(config_dir)


def test_load_invalid_cli_override_raises(tmp_path: Path) -> None:
    """An invalid CLI override is a ConfigError."""
    with pytest.raises(ConfigError, match="--deploy-mode"):
        load_deployment_config(_write_repo(tmp_path), deploy_mode_override="bogus")


def test_load_invalid_instance_mode_raises(tmp_path: Path) -> None:
    """An instance mode outside {prod, dev} is a ConfigError."""
    config_dir = _write_repo(
        tmp_path,
        instances_block='[instances.survival]\ncontainer = "mc-survival"\nmode = "bogus"\n',
    )
    with pytest.raises(ConfigError, match="mode"):
        load_deployment_config(config_dir)


def test_load_resource_pack_for_filtered_instance_is_skipped(tmp_path: Path) -> None:
    """A resource_pack whose instance is filtered is dropped silently (not an orphan error)."""
    config_dir = _write_repo(
        tmp_path,
        instances_block=(
            '[instances.survival]\ncontainer = "mc-survival"\nmode = "prod"\n\n'
            '[instances.latest]\ncontainer = "mc-latest"\nmode = "dev"\n\n'
            '[resource_pack.latest]\nfilename = "latest.zip"\nrequired = false\n'
        ),
    )
    cfg = load_deployment_config(config_dir)  # prod mode -> latest filtered
    assert cfg.resource_packs == {}


def test_load_resource_pack_for_active_instance_is_kept(tmp_path: Path) -> None:
    """A resource_pack whose instance matches the mode is loaded."""
    config_dir = _write_repo(
        tmp_path,
        instances_block=(
            '[instances.survival]\ncontainer = "mc-survival"\nmode = "prod"\n\n'
            '[instances.latest]\ncontainer = "mc-latest"\nmode = "dev"\n\n'
            '[resource_pack.survival]\nfilename = "survival.zip"\nrequired = true\n'
        ),
    )
    cfg = load_deployment_config(config_dir)  # prod mode -> survival active
    assert "survival" in cfg.resource_packs
    assert cfg.resource_packs["survival"].filename == "survival.zip"
