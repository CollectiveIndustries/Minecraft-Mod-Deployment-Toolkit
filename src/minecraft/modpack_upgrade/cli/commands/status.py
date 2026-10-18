# src/minecraft/modpack_upgrade/cli/commands/status.py

"""``minecraft-upgrade status`` - summarize pipeline gate state.

Reads the daemon's state directory and emits:

  * active release
  * last registry chain head
  * last verification record
  * current ``stable`` pointer target
  * slips available on disk
  * bundles available on disk

Always exits 0 (informational). Reads state files directly so the command
is side-effect-free -- no ``mkdir`` on a directory that does not yet exist.
"""

from __future__ import annotations

import json
from pathlib import Path

from LoggingCore import get_logger

from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config

__all__ = ["run"]

_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.status")


def _read_text_or_none(path: Path) -> str | None:
    """Return the stripped content of ``path``, or None."""
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _read_json_or_none(path: Path) -> dict | None:
    """Return the parsed JSON object at ``path``, or None."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _emit_pointer_target(slip_dir: Path, name: str) -> None:
    """Emit the current target of a pointer file."""
    path = slip_dir / name
    if not path.is_file():
        _log_cli.info(f"{name}: (not present)")
        return
    target = _read_text_or_none(path)
    _log_cli.info(f"{name}: {target or '(empty)'}")


def _emit_state(state_dir: Path) -> None:
    """Emit active release, last registry head, last verification."""
    active = _read_text_or_none(state_dir / "active-release.txt")
    head = _read_text_or_none(state_dir / "last-registry-head.txt")
    last_verify = _read_json_or_none(state_dir / "last-verification.json")

    _log_cli.info(f"active_release: {active or '(none)'}")
    _log_cli.info(f"last_registry_head: {head or '(none)'}")
    if last_verify:
        release = last_verify.get("release_id", "?")
        status = last_verify.get("status", "?")
        at = last_verify.get("at_utc", "?")
        _log_cli.info(f"last_verification: {release} ({status}) at {at}")
    else:
        _log_cli.info("last_verification: (none)")


def _emit_slips(slip_dir: Path) -> None:
    """Emit the slip filenames present in ``slip_dir``."""
    _log_cli.info("")
    _log_cli.info("slips:")
    if not slip_dir.is_dir():
        _log_cli.info("  (slip directory missing)")
        return
    slips = sorted(p.name for p in slip_dir.glob("v*.toml"))
    if not slips:
        _log_cli.info("  (none)")
        return
    for name in slips:
        _log_cli.info(f"  {name}")


def _emit_bundles(release_store: Path) -> None:
    """Emit the bundle directory names present in ``release_store``."""
    _log_cli.info("")
    _log_cli.info("bundles:")
    if not release_store.is_dir():
        _log_cli.info("  (bundle store missing)")
        return
    bundles = sorted(p.name for p in release_store.iterdir() if p.is_dir())
    if not bundles:
        _log_cli.info("  (none)")
        return
    for name in bundles:
        _log_cli.info(f"  {name}")


def run(*, config_dir: Path, project_root: Path) -> int:
    """Emit the current pipeline state. Always returns 0."""
    del project_root
    daemon_config = load_daemon_config(config_dir)

    _log_cli.info(f"daemon: minecraft-deploy v{daemon_config.daemon_version}")
    _log_cli.info(f"state_dir: {daemon_config.state_dir}")
    _log_cli.info(f"slip_dir: {daemon_config.slip_dir}")
    _log_cli.info(f"release_store: {daemon_config.release_store}")
    _log_cli.info("")

    _emit_state(daemon_config.state_dir)
    _log_cli.info("")
    _emit_pointer_target(daemon_config.slip_dir, "stable")
    _emit_slips(daemon_config.slip_dir)
    _emit_bundles(daemon_config.release_store)
    return 0
