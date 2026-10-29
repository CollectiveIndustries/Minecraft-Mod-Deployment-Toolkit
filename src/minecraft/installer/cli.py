# src/minecraft/installer/cli.py

"""Console-script entrypoints: install-prod, install-dev, rotate-secrets."""

from __future__ import annotations

import argparse
import traceback
from pathlib import Path

from LoggingCore import get_logger

from minecraft.common.cli_logging import configure as _configure_logging

from .errors import InstallerError
from .installer import install, rotate

__all__ = ["install_dev", "install_prod", "rotate_secrets"]

_log = get_logger("minecraft.deploy_pack.cli.installer")


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    project_root = Path(args.project_root).resolve() if args.project_root else Path.cwd().resolve()
    config_dir_raw = Path(args.config_dir)
    config_dir = config_dir_raw.resolve() if config_dir_raw.is_absolute() else (project_root / config_dir_raw).resolve()
    return project_root, config_dir


def _run_handler(mode_or_none: str | None, argv: list[str] | None) -> int:
    """Common entry for install-{prod,dev} and rotate-secrets."""
    if mode_or_none is not None:
        prog = f"install-{mode_or_none}"
        description = f"First-time install for {mode_or_none} mode."
    else:
        prog = "rotate-secrets"
        description = "Rotate every managed secret (RCON, Velocity)."

    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument(
        "--config-dir",
        default="config.d",
        help="Config directory (default: config.d)",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="Project root (default: current working directory)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging",
    )
    if mode_or_none is not None:
        parser.add_argument(
            "--no-up",
            action="store_true",
            help="Do not run 'docker compose up -d' after setup",
        )

    args = parser.parse_args(argv)
    _configure_logging(debug=args.debug)
    project_root, config_dir = _resolve_paths(args)

    try:
        if mode_or_none is not None:
            return install(
                mode_or_none,
                project_root,
                config_dir,
                up=not args.no_up,
                log=_log,
            )
        return rotate(project_root, config_dir, log=_log)
    except InstallerError as exc:
        _log.error(f"{exc}")
        return exc.exit_code
    except Exception as exc:
        _log.error(f"unexpected error: {exc}")
        if args.debug:
            traceback.print_exc()
        return 1


def install_prod(argv: list[str] | None = None) -> int:
    """First-time install for prod mode."""
    return _run_handler("prod", argv)


def install_dev(argv: list[str] | None = None) -> int:
    """First-time install for dev mode."""
    return _run_handler("dev", argv)


def rotate_secrets(argv: list[str] | None = None) -> int:
    """Rotate all secrets across the entire system."""
    return _run_handler(None, argv)
