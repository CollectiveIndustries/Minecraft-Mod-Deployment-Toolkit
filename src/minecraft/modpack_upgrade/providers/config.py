# src/minecraft/modpack_upgrade/providers/config.py

"""Provider configuration and registry construction.

Reads ``[providers]`` from ``config.d/upgrade.toml``. Values are
optional -- every key has a default -- so an operator who never touches
the provider section gets working Modrinth plus a CurseForge adapter
that raises a clear error only if actually used.

The CurseForge API key is *never* stored in the config file. The config
holds an environment variable name (``curseforge_api_key_env``), and
this module reads that variable at construction time (Section 18.1).

Usage tracking
--------------
``build_registry`` accepts an optional ``usage_tracker``. When
supplied, the default transport enforces API and byte caps on every
outgoing call. When ``transport`` is supplied explicitly, the tracker
is ignored -- the caller owns the transport.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from minecraft.modpack_upgrade.usage import UsageTracker

from .base import HttpTransport
from .curseforge import CurseForgeProvider
from .http import RequestsTransport
from .modrinth import ModrinthProvider
from .registry import ProviderRegistry

__all__ = ["ProviderConfig", "build_registry", "load_provider_config"]


_DEFAULT_MODRINTH_USER_AGENT = "minecraft-upgrade/1.3.1"
_DEFAULT_CURSEFORGE_API_KEY_ENV = "CURSEFORGE_API_KEY"
_DEFAULT_HTTP_TIMEOUT = 30.0
_DEFAULT_HTTP_MAX_RETRIES = 3
_DEFAULT_HTTP_BACKOFF_BASE = 1.0
_DEFAULT_MAX_ARTIFACT_BYTES = 500 * 1024 * 1024  # 500 MiB


@dataclass(frozen=True)
class ProviderConfig:
    """Fully-resolved provider configuration.

    ``curseforge_api_key`` is populated from the environment at load
    time and never written back to disk. Its presence in this dataclass
    is intentional and safe -- the dataclass is not logged, serialized,
    or persisted by any code in this package.
    """

    modrinth_user_agent: str
    curseforge_api_key: str | None
    http_timeout: float
    http_max_retries: int
    http_backoff_base: float
    max_artifact_bytes: int


def _read_providers_section(config_dir: Path) -> dict[str, object]:
    """Return the ``[providers]`` table from ``upgrade.toml``, or ``{}``."""
    toml_path = config_dir / "upgrade.toml"
    if not toml_path.is_file():
        return {}
    try:
        with open(toml_path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("providers")
    return section if isinstance(section, dict) else {}


def _get_str(section: Mapping[str, object], key: str, default: str) -> str:
    """Return ``section[key]`` as a string, or ``default``."""
    value = section.get(key)
    return str(value) if isinstance(value, str) and value else default


def _get_float(section: Mapping[str, object], key: str, default: float) -> float:
    """Return ``section[key]`` as a float, or ``default``."""
    value = section.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _get_int(section: Mapping[str, object], key: str, default: int) -> int:
    """Return ``section[key]`` as an int, or ``default``."""
    value = section.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _resolve_api_key(env_var_name: str, env: Mapping[str, str]) -> str | None:
    """Return the API key from the environment, or ``None``.

    An empty or missing variable is treated as unset; the key must not
    be an empty string (Section 18.1 - no ambiguous "set to nothing" state).
    """
    value = env.get(env_var_name, "")
    return value if value else None


def load_provider_config(
    config_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> ProviderConfig:
    """Read provider configuration from ``config_dir/upgrade.toml``.

    ``env`` defaults to ``os.environ``. Tests pass a fake mapping.

    Missing file, missing section, or missing keys all fall back to
    defaults. ``curseforge_api_key`` is ``None`` when the env var named
    by ``curseforge_api_key_env`` is unset or empty.
    """
    section = _read_providers_section(config_dir)
    effective_env = env if env is not None else os.environ

    api_key_env_name = _get_str(section, "curseforge_api_key_env", _DEFAULT_CURSEFORGE_API_KEY_ENV)

    return ProviderConfig(
        modrinth_user_agent=_get_str(section, "modrinth_user_agent", _DEFAULT_MODRINTH_USER_AGENT),
        curseforge_api_key=_resolve_api_key(api_key_env_name, effective_env),
        http_timeout=_get_float(section, "http_timeout", _DEFAULT_HTTP_TIMEOUT),
        http_max_retries=_get_int(section, "http_max_retries", _DEFAULT_HTTP_MAX_RETRIES),
        http_backoff_base=_get_float(section, "http_backoff_base", _DEFAULT_HTTP_BACKOFF_BASE),
        max_artifact_bytes=_get_int(section, "max_artifact_bytes", _DEFAULT_MAX_ARTIFACT_BYTES),
    )


def build_registry(
    config: ProviderConfig,
    *,
    transport: HttpTransport | None = None,
    usage_tracker: UsageTracker | None = None,
) -> ProviderRegistry:
    """Construct a :class:`ProviderRegistry` with Modrinth and CurseForge.

    ``transport`` defaults to :class:`RequestsTransport` configured from
    ``config``. Tests inject a fake transport.

    ``usage_tracker`` is passed through to the default transport. When
    ``transport`` is supplied explicitly, ``usage_tracker`` is ignored.
    """
    effective_transport = (
        transport
        if transport is not None
        else RequestsTransport(
            timeout=config.http_timeout,
            max_retries=config.http_max_retries,
            backoff_base=config.http_backoff_base,
            usage_tracker=usage_tracker,
        )
    )
    registry = ProviderRegistry()
    registry.register(ModrinthProvider(effective_transport, user_agent=config.modrinth_user_agent))
    registry.register(CurseForgeProvider(effective_transport, api_key=config.curseforge_api_key))
    return registry
