# src/minecraft/modpack_upgrade/cli/main.py

"""Entry point for the ``minecraft-upgrade`` CLI (spec v1.3 Section 15.1).

Dispatches subcommands to their implementations. Exit codes follow
Section 16.2:

  0  success
  1  operational failure
  2  CLI usage error
  3  config/schema/runtime/lock integrity error
  4  resolution failure
  5  validation gate failed
  6  approval blocked
  7  publication failure

All user-facing output flows through LoggingCore with a CLI-namespaced
logger (see :mod:`minecraft.common.cli_logging`). argparse's own usage
and help output is emitted by argparse itself and is unaffected.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from LoggingCore import get_logger

from minecraft.common.cli_logging import configure as _configure_cli_logging
from minecraft.modpack_upgrade import __version__

from .commands import (
    acknowledge,
    approve,
    build_inhouse,
    iterate,
    manifest,
    materialize,
    promote,
    publish,
    reject,
    runtime,
    status,
    stubs,
    update,
    verify,
)
from .errors import UpgradeCliError

__all__ = ["main"]

_log_cli = get_logger("minecraft.modpack_upgrade.cli.main")

_CommandHandler = Callable[[argparse.Namespace, Path, Path], int]


# ---------------------------------------------------------------------------
# Command dispatchers
# ---------------------------------------------------------------------------


def _cmd_verify(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the verify command."""
    return verify.run(config_dir=config_dir, project_root=project_root, tag=args.tag)


def _cmd_status(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the status command."""
    return status.run(config_dir=config_dir, project_root=project_root)


def _cmd_runtime_inspect(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the runtime inspect command."""
    return runtime.run(config_dir=config_dir, project_root=project_root)


def _cmd_manifest_check(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the manifest check command."""
    return manifest.run(config_dir=config_dir, project_root=project_root, path=args.path)


def _cmd_update(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the update command (spec Section 15.1.1)."""
    release_mode = getattr(args, "release_mode", None)
    if release_mode not in ("update", "upgrade"):
        raise UpgradeCliError(2, "update requires --release-mode {update|upgrade}")
    return update.run(
        config_dir=config_dir,
        project_root=project_root,
        release_mode=release_mode,
    )


def _cmd_acknowledge(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the acknowledge command (spec Section 15.1.2)."""
    return acknowledge.run(
        config_dir=config_dir,
        project_root=project_root,
        run_id=args.run_id,
        accept_losses=args.accept_losses,
        operator=args.operator,
    )


def _cmd_materialize(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the materialize command (spec Section 15.1.4)."""
    return materialize.run(config_dir=config_dir, project_root=project_root, run_id=args.run_id)


def _cmd_iterate(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the iterate command (spec Section 15.1.6)."""
    return iterate.run(config_dir=config_dir, project_root=project_root, run_id=args.run_id)


def _cmd_promote(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the promote command (spec Section 15.1.7)."""
    return promote.run(config_dir=config_dir, project_root=project_root, tag=args.tag)


def _cmd_publish(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the publish command (spec Section 15.1.8)."""
    return publish.run(
        config_dir=config_dir,
        project_root=project_root,
        release_id=args.release_id,
        verify_only=args.verify_only,
    )


def _parse_var_overrides(items: list[str]) -> dict[str, str]:
    """Parse repeated ``--var KEY=VALUE`` tokens into a mapping."""
    out: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not key or not sep:
            raise UpgradeCliError(2, f"--var must be KEY=VALUE, got {item!r}")
        out[key] = value
    return out


def _cmd_build_inhouse(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the build-inhouse command (repository-triggered build)."""
    legacy_revision = getattr(args, "legacy_revision", None)
    if legacy_revision is not None:
        _log_cli.warning("build-inhouse: second positional argument is ignored; put the git ref in config.d/build/<name>.yaml under 'ref:'")
    overrides = _parse_var_overrides(getattr(args, "var", []) or [])
    return build_inhouse.run(
        config_dir=config_dir,
        project_root=project_root,
        name=args.name,
        overrides=overrides,
    )


def _cmd_approve(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the approve command (spec Section 11.2, Section 11.4)."""
    return approve.run(
        config_dir=config_dir,
        project_root=project_root,
        run_id=args.run_id,
        scope=args.scope,
        operator=args.operator,
        interaction_id=args.interaction_id,
        reason=args.reason,
    )


def _cmd_reject(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
    """Dispatch to the reject command (spec Section 11.2, Section 11.4)."""
    return reject.run(
        config_dir=config_dir,
        project_root=project_root,
        run_id=args.run_id,
        scope=args.scope,
        operator=args.operator,
        interaction_id=args.interaction_id,
        reason=args.reason,
    )


def _cmd_stub(name: str, hint: str = "") -> _CommandHandler:
    """Return a stub handler for a not-yet-implemented command."""

    def _run(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
        return stubs.not_implemented(name, hint or None)

    return _run


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser with every subcommand."""
    parser = argparse.ArgumentParser(
        prog="minecraft-upgrade",
        description="Minecraft pack upgrade CLI (spec v1.3 Section 15.1)",
    )
    parser.add_argument(
        "--config-dir",
        default="config.d",
        help="Config directory (default: config.d)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"minecraft-upgrade {__version__}",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    # -- verify ------------------------------------------------------
    p_verify = sub.add_parser("verify", help="Read-only slip verification (spec Section 15.1)")
    p_verify.add_argument("--tag", required=True, help="Release tag to verify")
    p_verify.set_defaults(func=_cmd_verify)

    # -- status ------------------------------------------------------
    p_status = sub.add_parser("status", help="Summarize gates (spec Section 15.1)")
    p_status.set_defaults(func=_cmd_status)

    # -- runtime inspect ---------------------------------------------
    p_runtime = sub.add_parser("runtime", help="Runtime inspection")
    p_runtime_sub = p_runtime.add_subparsers(dest="runtime_action", metavar="<action>", required=True)
    p_runtime_inspect = p_runtime_sub.add_parser("inspect", help="Report effective runtime from Compose (spec Section 15.1)")
    p_runtime_inspect.set_defaults(func=_cmd_runtime_inspect)

    # -- manifest check ----------------------------------------------
    p_manifest = sub.add_parser("manifest", help="Manifest validation")
    p_manifest_sub = p_manifest.add_subparsers(dest="manifest_action", metavar="<action>", required=True)
    p_manifest_check = p_manifest_sub.add_parser("check", help="Validate manifest syntax and schema (spec Section 15.1)")
    p_manifest_check.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Manifest file (default: sync/modpack.manifest.toml)",
    )
    p_manifest_check.set_defaults(func=_cmd_manifest_check)

    # -- update ------------------------------------------------------
    p_update = sub.add_parser("update", help="Resolve versions; write report + state (spec 15.1.1)")
    p_update.add_argument(
        "--release-mode",
        required=True,
        choices=["update", "upgrade"],
        help="Runtime authority selector (spec 5.6)",
    )
    p_update.set_defaults(func=_cmd_update)

    # -- acknowledge -------------------------------------------------
    p_ack = sub.add_parser("acknowledge", help="Verify and record loss acceptance (spec 15.1.2)")
    p_ack.add_argument("run_id", help="Run ID to acknowledge")
    p_ack.add_argument(
        "--accept-losses",
        action="store_true",
        help="Non-interactive acceptance of all losses",
    )
    p_ack.add_argument(
        "--operator",
        default="interactive-cli",
        help="Operator identity recorded in acknowledgment.json",
    )
    p_ack.set_defaults(func=_cmd_acknowledge)

    # -- materialize -------------------------------------------------
    p_mat = sub.add_parser("materialize", help="Verify, download, seal slip (spec 15.1.4)")
    p_mat.add_argument("--run-id", required=True, help="Run ID to materialize")
    p_mat.set_defaults(func=_cmd_materialize)

    # -- iterate -----------------------------------------------------
    p_iter = sub.add_parser("iterate", help="Boot finalized candidate (spec 15.1.6)")
    p_iter.add_argument("--run-id", required=True, help="Run ID to iterate")
    p_iter.set_defaults(func=_cmd_iterate)

    # -- promote -----------------------------------------------------
    p_promote = sub.add_parser(
        "promote",
        help="Write slip and print git instructions (does not commit/tag/push)",
    )
    p_promote.add_argument("tag", help="Release tag to promote")
    p_promote.set_defaults(func=_cmd_promote)

    # -- publish -----------------------------------------------------
    p_publish = sub.add_parser("publish", help="Create immutable release bundle (spec 15.1.8)")
    p_publish.add_argument("release_id", help="Release ID to publish")
    p_publish.add_argument(
        "--verify-only",
        action="store_true",
        help=(
            "Verify an existing published release's chain and bundle instead "
            "of publishing. Uses trusted_keys from [registry].trusted_keys_path "
            "(default: config.d/trusted_keys.toml)."
        ),
    )
    p_publish.set_defaults(func=_cmd_publish)

    # -- build-inhouse -----------------------------------------------
    p_build = sub.add_parser(
        "build-inhouse",
        help="Clone + build an in-house mod from config.d/build/<name>.yaml",
    )
    p_build.add_argument("name", help="Build spec name (config.d/build/<name>.yaml)")
    p_build.add_argument(
        "legacy_revision",
        nargs="?",
        default=None,
        help=argparse.SUPPRESS,
    )
    p_build.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override a build variable (repeatable)",
    )
    p_build.set_defaults(func=_cmd_build_inhouse)

    # -- approve -----------------------------------------------------
    p_approve = sub.add_parser(
        "approve",
        help="Record an approval decision (spec Section 11.2, Section 11.4)",
    )
    p_approve.add_argument("run_id", help="Run ID to approve")
    p_approve.add_argument(
        "--scope",
        default="mod_set",
        choices=["mod_set", "world_migration", "velocity_integration"],
        help="Approval scope (default: mod_set)",
    )
    p_approve.add_argument(
        "--operator",
        default="interactive-cli",
        help="Operator identity recorded in the approval",
    )
    p_approve.add_argument(
        "--interaction-id",
        default=None,
        help="Discord interaction ID (for bot-originated approvals)",
    )
    p_approve.add_argument(
        "--reason",
        default=None,
        help="Optional free-form note attached to the approval",
    )
    p_approve.set_defaults(func=_cmd_approve)

    # -- reject ------------------------------------------------------
    p_reject = sub.add_parser(
        "reject",
        help="Record a rejection decision (spec Section 11.2, Section 11.4)",
    )
    p_reject.add_argument("run_id", help="Run ID to reject")
    p_reject.add_argument(
        "--scope",
        default="mod_set",
        choices=["mod_set", "world_migration", "velocity_integration"],
        help="Rejection scope (default: mod_set)",
    )
    p_reject.add_argument(
        "--operator",
        default="interactive-cli",
        help="Operator identity recorded in the rejection",
    )
    p_reject.add_argument(
        "--interaction-id",
        default=None,
        help="Discord interaction ID (for bot-originated rejections)",
    )
    p_reject.add_argument(
        "--reason",
        default=None,
        help="Optional free-form note attached to the rejection",
    )
    p_reject.set_defaults(func=_cmd_reject)

    # -- stubs (later slices) ----------------------------------------
    p_client = sub.add_parser("build-client", help="Produce candidate client ZIP")
    p_client.set_defaults(func=_cmd_stub("build-client"))

    p_snapshot = sub.add_parser("snapshot-world", help="Snapshot prod world to DEV")
    p_snapshot.add_argument(
        "world",
        nargs="?",
        default="survival",
        choices=["survival", "creative"],
        help="World to snapshot (default: survival)",
    )
    p_snapshot.set_defaults(func=_cmd_stub("snapshot-world"))

    p_validate = sub.add_parser("validate", help="Validation commands")
    p_validate_sub = p_validate.add_subparsers(dest="validate_action", metavar="<action>", required=True)
    p_validate_migration = p_validate_sub.add_parser("migration", help="Test snapshot against candidate")
    p_validate_migration.set_defaults(func=_cmd_stub("validate migration"))
    p_validate_velocity = p_validate_sub.add_parser("velocity", help="Isolated proxy integration")
    p_validate_velocity.set_defaults(func=_cmd_stub("validate velocity"))

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Process entry point. Returns the exit code."""
    if argv is None:
        argv = sys.argv[1:]

    _configure_cli_logging(debug=False)

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 0

    if not hasattr(args, "func"):
        parser.print_help()
        return 2

    config_dir = Path(args.config_dir).resolve()
    if config_dir.is_file():
        config_dir = config_dir.parent
    project_root = config_dir.parent

    try:
        return args.func(args, config_dir, project_root)
    except UpgradeCliError as exc:
        _log_cli.error(f"error: {exc.message}")
        return exc.exit_code
    except Exception as exc:
        _log_cli.error(f"error: {exc}")
        return 1
