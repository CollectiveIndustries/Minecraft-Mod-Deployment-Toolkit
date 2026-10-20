# src/minecraft/modpack_upgrade/cli/commands/build_inhouse.py

"""``minecraft-upgrade build-inhouse <name>`` -- clone, build, stage.

Reads ``config.d/build/<name>.yaml``, resolves variables against the
manifest and CLI overrides, then clones the repository, checks out the
declared ref, runs the mod's own build entrypoint, and copies the
declared artifacts into the warehouse (``sync/downloads/`` for jars,
``sync/downloads/.index/`` for ``.pw.toml``).

The tool does not know how to build Forge, NeoForge, or Fabric mods.
It runs a shell script and copies its output. This is deliberate:
platform-specific build logic belongs in the mod's repository, not in
the upgrade toolchain.
"""

from __future__ import annotations

from pathlib import Path

from LoggingCore import get_logger

from minecraft.modpack_upgrade.build import (
    BuildOutcome,
    load_build_spec,
    resolve_variables,
    run_build,
)
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.manifest import load_manifest

__all__ = ["run"]


_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.build_inhouse")


# ---------------------------------------------------------------------------
# Variable sources
# ---------------------------------------------------------------------------


def _manifest_vars(manifest_path: Path) -> dict[str, str]:
    """Return manifest-derived variables, or empty when unavailable.

    A missing or invalid manifest is a warning, not an error: a build
    spec that does not reference ``${minecraft_version}`` and friends
    can run without it. If a spec *does* reference an unavailable
    variable, :func:`resolve_variables` raises a clear error at that
    point.
    """
    if not manifest_path.is_file():
        _log_cli.warning(f"manifest not found at {manifest_path}; only YAML variables available")
        return {}
    try:
        manifest = load_manifest(manifest_path)
    except SchemaError as exc:
        _log_cli.warning(f"manifest invalid at {manifest_path}: {exc}; only YAML variables available")
        return {}
    return {
        "minecraft_version": manifest.minecraft_version,
        "loader": manifest.loader,
        "loader_version": manifest.loader_version,
    }


# ---------------------------------------------------------------------------
# Diagnostic output
# ---------------------------------------------------------------------------


def _emit_outcome(outcome: BuildOutcome) -> None:
    """Print the outcome to the CLI channel."""
    _log_cli.info(f"build: {outcome.name}")
    if outcome.success:
        _log_cli.info("status: ok")
        for dest_path in outcome.staged_artifacts:
            _log_cli.info(f"  staged: {dest_path}")
        return
    _log_cli.error(f"status: failed ({outcome.failure_message})")
    _log_cli.error(f"  clone kept for inspection: {outcome.clone_dir}")
    if outcome.stderr_excerpt:
        _log_cli.error("  stderr (tail):")
        for line in outcome.stderr_excerpt.splitlines():
            _log_cli.error(f"    {line}")


def _emit_inhouse_hint(outcome: BuildOutcome) -> None:
    """Hint the operator to mark every built jar as in-house."""
    jars = [Path(p).name for p in outcome.staged_artifacts if p.endswith(".jar")]
    if not jars:
        return
    _log_cli.info("note: add these filenames to config.d/upgrade.toml [in-house].filenames:")
    for name in jars:
        _log_cli.info(f"  {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    *,
    config_dir: Path,
    project_root: Path,
    name: str,
    overrides: dict[str, str] | None = None,
) -> int:
    """Execute ``minecraft-upgrade build-inhouse <name>``.

    Returns 0 on success, 1 on build failure, 3 on config/schema errors.
    """
    spec_path = config_dir / "build" / f"{name}.yaml"
    try:
        spec = load_build_spec(spec_path)
    except SchemaError as exc:
        raise UpgradeCliError(3, f"build spec error: {exc}") from exc

    manifest_path = project_root / "sync" / "modpack.manifest.toml"
    defaults = _manifest_vars(manifest_path)

    try:
        resolved = resolve_variables(spec, defaults, overrides or {})
    except SchemaError as exc:
        raise UpgradeCliError(3, f"variable resolution error: {exc}") from exc

    workspace_root = project_root / ".build-workspace"
    warehouse_root = project_root / "sync" / "downloads"

    outcome = run_build(
        resolved,
        project_root=project_root,
        workspace_root=workspace_root,
        warehouse_root=warehouse_root,
    )

    _emit_outcome(outcome)
    if not outcome.success:
        raise UpgradeCliError(1, outcome.failure_message or "build failed")

    _emit_inhouse_hint(outcome)
    return 0
