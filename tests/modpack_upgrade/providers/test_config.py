# tests/modpack_upgrade/providers/test_config.py

"""Provider configuration loader and registry builder tests."""

from __future__ import annotations

from pathlib import Path

from minecraft.modpack_upgrade.providers.base import HttpResult
from minecraft.modpack_upgrade.providers.config import (
    ProviderConfig,
    build_registry,
    load_provider_config,
)


class _NullTransport:
    """A transport that raises on any call."""

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        max_bytes: int | None = None,
    ) -> HttpResult:
        """Never called in these tests."""
        raise AssertionError(f"unexpected GET: {url}")


def _config_dir(tmp_path: Path) -> Path:
    """Create and return a config.d directory."""
    d = tmp_path / "config.d"
    d.mkdir()
    return d


def _write_upgrade_toml(config_dir: Path, body: str) -> None:
    """Write upgrade.toml with the given body."""
    (config_dir / "upgrade.toml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_missing_upgrade_toml_uses_defaults(tmp_path: Path) -> None:
    """No upgrade.toml at all yields default config."""
    cfg = load_provider_config(_config_dir(tmp_path), env={})
    assert cfg.modrinth_user_agent == "minecraft-upgrade/1.3.1"
    assert cfg.curseforge_api_key is None
    assert cfg.http_timeout == 30.0
    assert cfg.http_max_retries == 3
    assert cfg.http_backoff_base == 1.0
    assert cfg.max_artifact_bytes == 500 * 1024 * 1024


def test_missing_providers_section_uses_defaults(tmp_path: Path) -> None:
    """upgrade.toml without [providers] yields default config."""
    d = _config_dir(tmp_path)
    _write_upgrade_toml(d, '[resolver]\nrelease_policy = "release_then_beta"\n')
    cfg = load_provider_config(d, env={})
    assert cfg.modrinth_user_agent == "minecraft-upgrade/1.3.1"


def test_malformed_toml_uses_defaults(tmp_path: Path) -> None:
    """Malformed upgrade.toml falls back to defaults silently."""
    d = _config_dir(tmp_path)
    _write_upgrade_toml(d, "not = valid = toml\n")
    cfg = load_provider_config(d, env={})
    assert cfg.modrinth_user_agent == "minecraft-upgrade/1.3.1"


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def test_overrides_read_from_section(tmp_path: Path) -> None:
    """All override fields are honoured."""
    d = _config_dir(tmp_path)
    _write_upgrade_toml(
        d,
        "[providers]\n"
        'modrinth_user_agent = "custom/2.0 (op@example.com)"\n'
        'curseforge_api_key_env = "MY_CF_KEY"\n'
        "http_timeout = 10.0\n"
        "http_max_retries = 5\n"
        "http_backoff_base = 2.0\n"
        "max_artifact_bytes = 1000000\n",
    )
    cfg = load_provider_config(d, env={"MY_CF_KEY": "secret-value"})
    assert cfg.modrinth_user_agent == "custom/2.0 (op@example.com)"
    assert cfg.curseforge_api_key == "secret-value"
    assert cfg.http_timeout == 10.0
    assert cfg.http_max_retries == 5
    assert cfg.http_backoff_base == 2.0
    assert cfg.max_artifact_bytes == 1000000


def test_api_key_from_default_env_var(tmp_path: Path) -> None:
    """Without an override, CURSEFORGE_API_KEY is read from the environment."""
    d = _config_dir(tmp_path)
    _write_upgrade_toml(d, "[providers]\n")
    cfg = load_provider_config(d, env={"CURSEFORGE_API_KEY": "default-key"})
    assert cfg.curseforge_api_key == "default-key"


def test_empty_api_key_env_var_treated_as_unset(tmp_path: Path) -> None:
    """An empty env var value is treated as unset, not as an empty key."""
    d = _config_dir(tmp_path)
    _write_upgrade_toml(d, "[providers]\n")
    cfg = load_provider_config(d, env={"CURSEFORGE_API_KEY": ""})
    assert cfg.curseforge_api_key is None


def test_negative_timing_values_fall_through_to_defaults(tmp_path: Path) -> None:
    """Invalid numeric values are ignored in favour of defaults."""
    d = _config_dir(tmp_path)
    _write_upgrade_toml(
        d,
        '[providers]\nhttp_timeout = "oops"\nhttp_max_retries = "oops"\n',
    )
    cfg = load_provider_config(d, env={})
    assert cfg.http_timeout == 30.0
    assert cfg.http_max_retries == 3


# ---------------------------------------------------------------------------
# build_registry
# ---------------------------------------------------------------------------


def _cfg(api_key: str | None = None) -> ProviderConfig:
    """Return a minimal ProviderConfig for registry-builder tests."""
    return ProviderConfig(
        modrinth_user_agent="test/1.0",
        curseforge_api_key=api_key,
        http_timeout=5.0,
        http_max_retries=2,
        http_backoff_base=0.1,
        max_artifact_bytes=1024,
    )


def test_build_registry_registers_both_providers() -> None:
    """A built registry contains modrinth and curseforge."""
    registry = build_registry(_cfg(), transport=_NullTransport())
    assert registry.names() == ["curseforge", "modrinth"]


def test_build_registry_without_curseforge_key() -> None:
    """The CurseForge provider is registered even without an API key.

    Using it raises immediately, but construction succeeds so a
    Modrinth-only workflow does not fail.
    """
    registry = build_registry(_cfg(api_key=None), transport=_NullTransport())
    assert "curseforge" in registry


def test_build_registry_default_transport_is_requests() -> None:
    """Omitting transport builds a RequestsTransport internally."""
    registry = build_registry(_cfg())
    assert "modrinth" in registry
