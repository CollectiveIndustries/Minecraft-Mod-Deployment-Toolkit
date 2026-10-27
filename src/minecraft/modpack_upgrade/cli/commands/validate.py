# src/minecraft/modpack_upgrade/cli/commands/validate.py

"""``minecraft-upgrade validate {migration|velocity}`` -- validation gates (Section 8.4, Section 8.5).

Both commands verify preconditions and report what automation can and
cannot prove against the current environment. They are honest about
their coverage:

**migration** verifies:
  - a snapshot exists and its manifest parses and verifies
  - a materialized candidate exists
  - the candidate boots against the integration container (via
    `deploy-pack --integration --release-pointer latest-slip`)

**migration does NOT verify:**
  - the candidate boots against the *snapshot world* (would require
    remounting the integration container's world directory, which is
    environment-specific). Operators must perform this manually after
    the automatic precondition + boot checks pass.

**velocity** verifies:
  - a Velocity service is declared in compose
  - its container is running and healthy

**velocity does NOT verify:**
  - backend handshake, forwarding, or identity. Those require a live
    player connection through the proxy and are exercised manually by
    the operator per Section 8.5.

Both commands exit 0 when the checks they can perform pass, even when
the checks they cannot perform are explicitly reported as "manual
required." That is the honest contract: automation reduces the manual
burden without pretending to eliminate it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

from LoggingCore import get_logger

from minecraft.deploy_pack.config_model import load_compose
from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.deploy_pack.docker_runtime import DockerRuntime
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError

__all__ = ["run_migration", "run_velocity"]

_log = get_logger(__name__)
_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.validate")

_SNAPSHOT_MANIFEST = "snapshot-manifest.json"


def _load_snapshot_config(config_dir: Path) -> dict:
    """Load ``[world_snapshot]`` to find ``dest_root``."""
    toml_path = config_dir / "upgrade.toml"
    if not toml_path.is_file():
        raise UpgradeCliError(3, f"upgrade.toml not found: {toml_path}")
    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UpgradeCliError(3, f"could not read {toml_path}: {exc}") from exc
    section = data.get("world_snapshot")
    dest_root_raw = section.get("dest_root", "~/minecraft-dev/snapshots") if isinstance(section, dict) else "~/minecraft-dev/snapshots"
    return {"dest_root": Path(str(dest_root_raw)).expanduser()}


def _find_snapshot(dest_root: Path, snapshot_id: str | None) -> Path:
    """Return the snapshot directory (specified or most recent)."""
    if not dest_root.is_dir():
        raise UpgradeCliError(3, f"snapshot destination not found: {dest_root}")
    if snapshot_id is not None:
        candidate = dest_root / snapshot_id
        if not candidate.is_dir():
            raise UpgradeCliError(3, f"snapshot not found: {candidate}")
        return candidate
    snapshots = sorted(
        [d for d in dest_root.iterdir() if d.is_dir() and (d / _SNAPSHOT_MANIFEST).is_file()],
        key=lambda p: (p / _SNAPSHOT_MANIFEST).stat().st_mtime,
        reverse=True,
    )
    if not snapshots:
        raise UpgradeCliError(3, f"no snapshots found under {dest_root}")
    if len(snapshots) > 1:
        _log_cli.warning(f"multiple snapshots found; using most recent: {snapshots[0].name}")
    return snapshots[0]


def _verify_manifest(snapshot_dir: Path) -> None:
    """Verify the snapshot manifest parses and each file's hash matches."""
    manifest_path = snapshot_dir / _SNAPSHOT_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise UpgradeCliError(1, f"snapshot manifest unreadable: {exc}") from exc
    files = manifest.get("files", [])
    if not isinstance(files, list):
        raise UpgradeCliError(1, "snapshot manifest 'files' is not a list")
    world_dir = snapshot_dir / "world"
    for entry in files:
        rel = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(rel, str) or not isinstance(expected, str):
            raise UpgradeCliError(1, f"snapshot manifest entry malformed: {entry!r}")
        full = world_dir / rel
        if not full.is_file():
            raise UpgradeCliError(1, f"snapshot file missing: {rel}")
        actual = hashlib.sha256(full.read_bytes()).hexdigest()
        if actual != expected:
            raise UpgradeCliError(1, f"snapshot hash mismatch: {rel} expected={expected} actual={actual}")


def _run_deploy_pack_integration(config_dir: Path, project_root: Path, timeout: float = 1800.0) -> None:
    """Shell to ``deploy-pack --integration --release-pointer latest-slip``."""
    try:
        result = subprocess.run(
            [
                "deploy-pack",
                "--integration",
                "--release-pointer",
                "latest-slip",
                "--server",
                "--config-dir",
                str(config_dir),
            ],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise UpgradeCliError(1, f"deploy-pack not available: {exc}") from exc
    except subprocess.TimeoutExpired:
        raise UpgradeCliError(1, f"integration deploy timed out after {timeout:.0f}s") from None
    if result.returncode != 0:
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        raise UpgradeCliError(1, f"integration deploy failed (exit {result.returncode}): {combined.strip()[-500:]}")


def run_migration(
    *,
    config_dir: Path,
    project_root: Path,
    snapshot_id: str | None = None,
) -> int:
    """Validate migration: precondition checks + integration boot."""
    config = _load_snapshot_config(config_dir)
    snapshot_dir = _find_snapshot(config["dest_root"], snapshot_id)
    _log_cli.info(f"snapshot: {snapshot_dir}")

    _log_cli.info("verifying snapshot manifest...")
    _verify_manifest(snapshot_dir)
    _log_cli.info("  OK")

    daemon_config = load_daemon_config(config_dir)
    candidates_root = Path(daemon_config.state_dir) / "candidates"
    candidates = [d for d in candidates_root.iterdir() if (d / "packing_slip.toml").is_file()] if candidates_root.is_dir() else []
    if not candidates:
        raise UpgradeCliError(3, f"no materialized candidate under {candidates_root}; run `materialize` first")
    _log_cli.info(f"candidate: {candidates[0].name}")

    _log_cli.info("booting candidate via deploy-pack --integration...")
    _run_deploy_pack_integration(config_dir, project_root)
    _log_cli.info("  OK")

    _log_cli.info("")
    _log_cli.warning(
        "migration validation: PARTIAL\n"
        f"  The candidate booted successfully via the integration container.\n"
        f"  The candidate did NOT boot against the snapshot world at {snapshot_dir / 'world'}.\n"
        "  That verification is environment-specific and must be done manually:"
    )
    _log_cli.warning("    1. Stop the integration container.")
    _log_cli.warning(f"    2. Point its world mount at {snapshot_dir / 'world'}.")
    _log_cli.warning("    3. Restart and observe health.")
    _log_cli.warning("    4. Attach to the container and verify gameplay per Section 8.4.")
    return 0


def _find_velocity_service(compose_path: Path) -> object | None:
    """Return the first compose service whose name contains 'velocity'."""
    result = load_compose(compose_path)
    if not result.ok or result.file is None:
        return None
    for svc in result.file.services.values():
        if "velocity" in svc.name.lower():
            return svc
    return None


def run_velocity(*, config_dir: Path, project_root: Path) -> int:
    """Validate velocity: proxy container health check."""
    toml_path = config_dir / "deploy_pack.toml"
    compose_path = project_root / "docker-compose.yml"
    if toml_path.is_file():
        try:
            with open(toml_path, "rb") as fh:
                data = tomllib.load(fh)
            docker = data.get("docker", {})
            if isinstance(docker, dict) and docker.get("compose_file"):
                raw = Path(str(docker["compose_file"]))
                compose_path = raw if raw.is_absolute() else project_root / raw
        except (OSError, tomllib.TOMLDecodeError):
            pass

    if not compose_path.is_file():
        raise UpgradeCliError(3, f"compose file not found: {compose_path}")

    svc = _find_velocity_service(compose_path)
    if svc is None:
        _log_cli.info("velocity: no velocity service declared in compose; nothing to validate")
        return 0

    container = svc.container_name
    if not container:
        raise UpgradeCliError(3, f"velocity service {svc.name!r} has no container_name")

    _log_cli.info(f"velocity container: {container}")
    runtime = DockerRuntime()
    state = runtime.inspect(container)
    if not state.exists:
        raise UpgradeCliError(1, f"velocity container not found: {container}")
    if not state.running:
        raise UpgradeCliError(1, f"velocity container not running: {container} (status={state.status!r})")
    if state.health != "healthy":
        raise UpgradeCliError(1, f"velocity container not healthy: {container} (health={state.health!r})")

    _log_cli.info("  container healthy")
    _log_cli.info("")
    _log_cli.warning(
        "velocity validation: PARTIAL\n"
        "  The proxy container is running and healthy.\n"
        "  Backend handshake, forwarding, and identity are NOT verified.\n"
        "  Those require a live player connection through the proxy and must\n"
        "  be exercised manually per Section 8.5."
    )
    return 0
