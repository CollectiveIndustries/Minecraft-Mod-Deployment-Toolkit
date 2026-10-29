# src/minecraft/installer/installer.py

"""Orchestration: install a mode's stack, or rotate all secrets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .compose import bring_up, derive_instance_roots
from .config import InstallerConfig, load_installer_config
from .errors import ComposeError, InstallerError
from .rcon import configure_rcon, configure_server_properties
from .secrets import ensure_secret_file
from .velocity import update_pcf_forwarding_secret

__all__ = ["install", "rotate"]

_VALID_MODES = frozenset({"prod", "dev"})


def _compose_path_for_mode(project_root: Path, mode: str) -> Path:
    if mode == "prod":
        return project_root / "docker-compose.yml"
    return project_root / f"docker-compose.{mode}.yml"


def install(
    mode: str,
    project_root: Path,
    config_dir: Path,
    *,
    up: bool,
    log: Any,
) -> int:
    """Set up secrets and bring the stack up for one mode."""
    if mode not in _VALID_MODES:
        raise InstallerError(f"invalid mode: {mode!r} (expected 'prod' or 'dev')")

    config = load_installer_config(project_root, config_dir)
    log.info(f"Installing {mode} mode in {project_root}")

    # 1. RCON secret
    rcon_path = config.secrets_dir / config.rcon_secret_name
    rcon_secret, rcon_created = ensure_secret_file(rcon_path, rotate=False)
    state = "created" if rcon_created else "existing"
    log.info(f"RCON secret at {rcon_path}: {state}")

    # 2. Derive instance roots from compose
    compose_path = _compose_path_for_mode(project_root, mode)
    if not compose_path.is_file():
        raise ComposeError(f"compose file not found: {compose_path}")
    roots = derive_instance_roots(compose_path, log=log)
    log.info(f"Derived {len(roots)} instance root(s) from {compose_path.name}")

    # 3. Configure RCON in each server.properties
    configure_rcon(roots, rcon_secret, log=log)

    # 4. Configure Velocity if enabled
    if config.velocity is not None:
        _install_velocity(config, log=log)
    else:
        log.debug("Velocity not configured; skipping velocity secrets")

    # 5. Bring the stack up
    if up:
        bring_up(compose_path, log=log)
    else:
        log.info("--no-up: skipping docker compose up")

    log.info(f"Install complete for {mode} mode")
    return 0


def _install_velocity(config: InstallerConfig, *, log: Any) -> None:
    velocity = config.velocity
    assert velocity is not None
    velocity_secret, created = ensure_secret_file(velocity.forwarding_secret_path, rotate=False)
    state = "created" if created else "existing"
    log.info(f"Velocity forwarding secret at {velocity.forwarding_secret_path}: {state}")
    for pcf in velocity.pcf_paths:
        changed = update_pcf_forwarding_secret(pcf, velocity_secret)
        result = "updated" if changed else "unchanged"
        log.info(f"[velocity] {pcf}: {result}")


def rotate(project_root: Path, config_dir: Path, *, log: Any) -> int:
    """Rotate every managed secret and update every consumer."""
    config = load_installer_config(project_root, config_dir)
    log.warning(f"Rotating all secrets in {project_root}")

    # 1. Rotate RCON
    rcon_path = config.secrets_dir / config.rcon_secret_name
    rcon_secret, _ = ensure_secret_file(rcon_path, rotate=True)
    log.warning(f"RCON secret regenerated at {rcon_path}")

    # 2. Update server.properties for every mode that has a compose file
    touched_props = 0
    for mode in ("prod", "dev"):
        compose_path = _compose_path_for_mode(project_root, mode)
        if not compose_path.is_file():
            log.debug(f"skipping {mode}: {compose_path} not found")
            continue
        try:
            roots = derive_instance_roots(compose_path, log=log)
        except ComposeError as exc:
            log.warning(f"could not derive roots for {mode}: {exc}")
            continue
        for container_name, root in roots:
            props_path = root / "server.properties"
            changed = configure_server_properties(props_path, rcon_secret)
            if changed:
                touched_props += 1
                log.info(f"[{mode}/{container_name}] updated {props_path}")

    # 3. Rotate Velocity if configured
    if config.velocity is not None:
        velocity_secret, _ = ensure_secret_file(config.velocity.forwarding_secret_path, rotate=True)
        log.warning(f"Velocity forwarding secret regenerated at {config.velocity.forwarding_secret_path}")
        for pcf in config.velocity.pcf_paths:
            changed = update_pcf_forwarding_secret(pcf, velocity_secret)
            if changed:
                log.info(f"[velocity] updated {pcf}")

    log.warning(f"Rotation complete. Restart containers to pick up the new values ({touched_props} properties file(s) updated).")
    return 0
