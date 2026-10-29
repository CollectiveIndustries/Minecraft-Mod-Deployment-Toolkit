# src/minecraft/installer/config.py

"""Installer configuration: what to install and where.

Reads ``[installer]`` from ``config.d/deploy_pack.toml``. Every key is
optional; the defaults match the layout the delivery spec describes.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

__all__ = ["InstallerConfig", "VelocityConfig", "load_installer_config"]

_DEFAULT_SECRETS_DIR = "./secrets"
_DEFAULT_RCON_NAME = "rcon_password"


@dataclass(frozen=True)
class VelocityConfig:
    """Paths for Velocity secret installation."""

    forwarding_secret_path: Path
    pcf_paths: tuple[Path, ...]


@dataclass(frozen=True)
class InstallerConfig:
    """Everything the installer needs to know from the project config."""

    project_root: Path
    secrets_dir: Path
    rcon_secret_name: str
    velocity: VelocityConfig | None


def load_installer_config(project_root: Path, config_dir: Path) -> InstallerConfig:
    """Load ``[installer]`` from ``deploy_pack.toml``; missing keys use defaults."""
    toml_path = config_dir / "deploy_pack.toml"
    data: dict = {}
    if toml_path.is_file():
        try:
            with toml_path.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"could not read {toml_path}: {exc}") from exc

    installer_section = data.get("installer") or {}
    if not isinstance(installer_section, dict):
        raise ConfigError("[installer] must be a table")

    secrets_dir_raw = installer_section.get("secrets_dir", _DEFAULT_SECRETS_DIR)
    if not isinstance(secrets_dir_raw, str):
        raise ConfigError("[installer].secrets_dir must be a string")
    secrets_dir = _resolve(secrets_dir_raw, project_root)

    rcon_raw = installer_section.get("rcon_secret_name", _DEFAULT_RCON_NAME)
    if not isinstance(rcon_raw, str):
        raise ConfigError("[installer].rcon_secret_name must be a string")

    velocity_section = installer_section.get("velocity")
    velocity: VelocityConfig | None = None
    if velocity_section is not None:
        if not isinstance(velocity_section, dict):
            raise ConfigError("[installer.velocity] must be a table")
        velocity = _load_velocity_section(velocity_section, project_root)

    return InstallerConfig(
        project_root=project_root,
        secrets_dir=secrets_dir,
        rcon_secret_name=rcon_raw,
        velocity=velocity,
    )


def _load_velocity_section(section: dict, project_root: Path) -> VelocityConfig:
    raw_secret = section.get("forwarding_secret_path")
    if not isinstance(raw_secret, str) or not raw_secret:
        raise ConfigError("[installer.velocity].forwarding_secret_path is required")
    raw_pcf = section.get("pcf_paths")
    if not isinstance(raw_pcf, list) or not raw_pcf:
        raise ConfigError("[installer.velocity].pcf_paths must be a non-empty list")
    pcf_paths: list[Path] = []
    for entry in raw_pcf:
        if not isinstance(entry, str):
            raise ConfigError("[installer.velocity].pcf_paths entries must be strings")
        pcf_paths.append(_resolve(entry, project_root))
    return VelocityConfig(
        forwarding_secret_path=_resolve(raw_secret, project_root),
        pcf_paths=tuple(pcf_paths),
    )


def _resolve(raw: str, project_root: Path) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = project_root / p
    return p.resolve()
