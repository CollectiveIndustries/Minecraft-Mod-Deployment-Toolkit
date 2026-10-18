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

Commands not yet implemented exit 1 with a message on stderr.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from minecraft.modpack_upgrade import __version__

from .commands import manifest, runtime, status, stubs, verify
from .errors import UpgradeCliError

__all__ = ["main"]


_CommandHandler = Callable[[argparse.Namespace, Path, Path], int]


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


def _cmd_stub(name: str, hint: str = "") -> _CommandHandler:
    """Return a stub handler for a not-yet-implemented command."""

    def _run(args: argparse.Namespace, config_dir: Path, project_root: Path) -> int:
        return stubs.not_implemented(name, hint or None)

    return _run


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
    p_manifest_check = p_manifest_sub.add_parser("check", help="Validate manifest syntax (spec Section 15.1)")
    p_manifest_check.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Manifest file (default: sync/modpack.manifest.toml)",
    )
    p_manifest_check.set_defaults(func=_cmd_manifest_check)

    # -- stubs -------------------------------------------------------
    p_update = sub.add_parser("update", help="Query providers, resolve, write candidate slip")
    p_update.set_defaults(func=_cmd_stub("update"))

    p_build = sub.add_parser("build-inhouse", help="Build an in-house mod JAR")
    p_build.add_argument("mod", help="Mod slug")
    p_build.add_argument("revision", help="Git revision to build")
    p_build.set_defaults(func=_cmd_stub("build-inhouse"))

    p_materialize = sub.add_parser("materialize", help="Download and verify artifacts")
    p_materialize.set_defaults(func=_cmd_stub("materialize"))

    p_iterate = sub.add_parser("iterate", help="Run DEV boot loop")
    p_iterate.set_defaults(func=_cmd_stub("iterate"))

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

    p_publish = sub.add_parser("publish", help="Create immutable release bundle")
    p_publish.add_argument("release_id", help="Release ID to publish")
    p_publish.set_defaults(func=_cmd_stub("publish"))

    p_promote = sub.add_parser(
        "promote",
        help="Write slip and print git instructions (does not commit/tag/push)",
    )
    p_promote.add_argument("tag", help="Release tag to promote")
    p_promote.set_defaults(func=_cmd_stub("promote"))

    return parser


def main(argv: list[str] | None = None) -> int:
    """Process entry point. Returns the exit code."""
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 0

    if not hasattr(args, "func"):
        parser.print_help(sys.stderr)
        return 2

    config_dir = Path(args.config_dir).resolve()
    if config_dir.is_file():
        config_dir = config_dir.parent
    project_root = config_dir.parent

    try:
        return args.func(args, config_dir, project_root)
    except UpgradeCliError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
