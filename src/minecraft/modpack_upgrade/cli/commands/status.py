# src/minecraft/modpack_upgrade/cli/commands/status.py

"""``minecraft-upgrade status`` - summarize pipeline gate state.

Reads the daemon's state directory and prints:

  * active release
  * last registry chain head
  * last verification record
  * current ``stable`` pointer target
  * slips available on disk
  * bundles available on disk

Always exits 0 (informational). Reads the state files directly so the
command is side-effect-free - no ``mkdir`` on a directory that does not
yet exist.
"""

from __future__ import annotations

import json
from pathlib import Path

from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config

__all__ = ["run"]


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


def _print_pointer_target(slip_dir: Path, name: str) -> None:
    """Print the current target of a pointer file."""
    path = slip_dir / name
    if not path.is_file():
        print(f"{name}: (not present)")
        return
    target = _read_text_or_none(path)
    print(f"{name}: {target or '(empty)'}")


def _print_state(state_dir: Path) -> None:
    """Print active release, last registry head, last verification."""
    active = _read_text_or_none(state_dir / "active-release.txt")
    head = _read_text_or_none(state_dir / "last-registry-head.txt")
    last_verify = _read_json_or_none(state_dir / "last-verification.json")

    print(f"active_release: {active or '(none)'}")
    print(f"last_registry_head: {head or '(none)'}")
    if last_verify:
        release = last_verify.get("release_id", "?")
        status = last_verify.get("status", "?")
        at = last_verify.get("at_utc", "?")
        print(f"last_verification: {release} ({status}) at {at}")
    else:
        print("last_verification: (none)")


def _print_slips(slip_dir: Path) -> None:
    """Print the slip filenames present in ``slip_dir``."""
    print()
    print("slips:")
    if not slip_dir.is_dir():
        print("  (slip directory missing)")
        return
    slips = sorted(p.name for p in slip_dir.glob("v*.toml"))
    if not slips:
        print("  (none)")
        return
    for name in slips:
        print(f"  {name}")


def _print_bundles(release_store: Path) -> None:
    """Print the bundle directory names present in ``release_store``."""
    print()
    print("bundles:")
    if not release_store.is_dir():
        print("  (bundle store missing)")
        return
    bundles = sorted(p.name for p in release_store.iterdir() if p.is_dir())
    if not bundles:
        print("  (none)")
        return
    for name in bundles:
        print(f"  {name}")


def run(*, config_dir: Path, project_root: Path) -> int:
    """Print the current pipeline state. Always returns 0."""
    del project_root  # resolved from config_dir
    daemon_config = load_daemon_config(config_dir)

    print(f"daemon: minecraft-deploy v{daemon_config.daemon_version}")
    print(f"state_dir: {daemon_config.state_dir}")
    print(f"slip_dir: {daemon_config.slip_dir}")
    print(f"release_store: {daemon_config.release_store}")
    print()

    _print_state(daemon_config.state_dir)
    print()
    _print_pointer_target(daemon_config.slip_dir, "stable")
    _print_slips(daemon_config.slip_dir)
    _print_bundles(daemon_config.release_store)
    return 0
