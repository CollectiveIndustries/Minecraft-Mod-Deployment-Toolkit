#!/usr/bin/env python3
"""
deploy_pack.py
Generates a client ZIP archive (server mode) OR deploys directly to a MultiMC instance (client mode).
Uses ConfigCore and LoggingCore (mandatory dependencies).

Configuration is read from:
  - Default: ./config.d/deploy_pack.{toml,yaml,yml}
  - Override via --config-dir <path>
  - Override via environment variable DEPLOYPACK_CONFIG_DIR
"""

import argparse
import datetime
import fnmatch
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from ConfigCore import ConfigManager
from LoggingCore import get_logger, setup_logging


# ----------------------------------------------------------------------
# Helper: find the first existing config file in config.d/
# ----------------------------------------------------------------------
def find_config_file(config_dir: Path) -> Path | None:
    """Return the path to the first existing config file in config_dir."""
    extensions = [".toml", ".yaml", ".yml"]
    for ext in extensions:
        candidate = config_dir / f"deploy_pack{ext}"
        if candidate.is_file():
            return candidate
    return None


# ----------------------------------------------------------------------
# Core functions
# ----------------------------------------------------------------------
def get_exclude_patterns(exclude_file_path: Path, logger):
    patterns = []
    if not exclude_file_path.is_file():
        logger.warning(f"Exclude file not found: {exclude_file_path}")
        return patterns
    with exclude_file_path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    return patterns


def copy_directory_contents(src: Path, dst: Path, logger):
    """Copy all contents of src into dst (merging directories)."""
    if not src.is_dir():
        raise NotADirectoryError(f"Source not found: {src}")
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        dest_item = dst / item.name
        if item.is_dir():
            shutil.copytree(item, dest_item, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest_item)
    logger.debug(f"Copied {src} -> {dst}")


def copy_with_exclusions(src: Path, dst: Path, exclude_patterns: list, logger):
    """Copy contents of src into dst, skipping files matching any exclude pattern."""
    if not src.is_dir():
        raise NotADirectoryError(f"Source not found: {src}")
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        for file in files:
            full_path = Path(root) / file
            rel_path = rel_root / file
            # Check exclusions
            excluded = False
            for pattern in exclude_patterns:
                if fnmatch.fnmatch(str(rel_path), pattern):
                    excluded = True
                    break
            if excluded:
                logger.debug(f"Skipping excluded: {rel_path}")
                continue
            dest_path = dst / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(full_path, dest_path)
    logger.info(f"Copied with exclusions to {dst}")


def create_zip_from_staging(staging_dir: Path, output_zip: Path, exclude_patterns: list, logger):
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(staging_dir):
            for file in files:
                full_path = Path(root) / file
                rel_path = full_path.relative_to(staging_dir)
                excluded = False
                for pattern in exclude_patterns:
                    if fnmatch.fnmatch(str(rel_path), pattern):
                        excluded = True
                        break
                if excluded:
                    logger.debug(f"Skipping excluded: {rel_path}")
                    continue
                zf.write(full_path, arcname=str(rel_path))
    logger.info(f"Created zip: {output_zip}")


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    # 1. Parse command-line flags (--server / --client / --config-dir)
    parser = argparse.ArgumentParser(description="Deploy client pack")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--server", action="store_true", help="Server mode: create ZIP (default)")
    group.add_argument("--client", action="store_true", help="Client mode: deploy to MultiMC instance")
    parser.add_argument("--config-dir", type=str, default=None,
                        help="Path to configuration directory (default: config.d)")
    args, remaining = parser.parse_known_args()

    # Determine mode
    mode = "client" if args.client else "server"   # default to server

    # Determine config directory (CLI > env > default)
    config_dir = Path(args.config_dir) if args.config_dir else \
                 Path(os.environ.get("DEPLOYPACK_CONFIG_DIR", "config.d"))

    # Locate config file
    config_path = find_config_file(config_dir)
    if config_path is None:
        print(f"WARNING: No config file found in {config_dir}. Using defaults.", file=sys.stderr)

    # Build configuration manager
    mgr = ConfigManager()
    if config_path is not None:
        mgr.file(config_path)

    # Environment variables (prefix DEPLOYPACK_)
    mgr.env("DEPLOYPACK")

    # Command-line overrides (only the remaining args, not the mode flags)
    if remaining:
        mgr.cli(remaining)

    # Load config
    config = mgr.load()

    # Extract settings with defaults
    def get_path(key: str, default: str) -> Path:
        val = config.get(key)
        return Path(val) if val is not None else Path(default)

    sync_root = get_path("sync_root", "/home/minecraft/minecraft/sync")
    live_server = get_path("live_server", "/home/minecraft/minecraft/server")
    www_dir = get_path("www_dir", "/home/minecraft/minecraft/www")
    exclude_file = get_path("exclude_file", "/home/minecraft/nfs/sync/.rsync_exclude")
    output_filename = config.get("output_filename", "minecraft_client_{date}.zip")

    # MultiMC settings (only used in client mode)
    multimc_base = config.get("multimc_base")
    if multimc_base is None:
        # default: ~/.local/share/multimc/instances
        multimc_base = Path.home() / ".local/share/multimc/instances"
    else:
        multimc_base = Path(multimc_base)
    instance_name = config.get("instance_name")  # required for client mode

    # Setup logging
    log_config = config.get("logging")
    if log_config is None:
        log_config = {
            "color": True,
            "handlers": [
                {"type": "console", "color": True},
                {"type": "file",
                 "path": "logs/deploy_pack.log",
                 "max_bytes": 10_485_760,
                 "backup_count": 5}
            ]
        }
    setup_logging(log_config)
    logger = get_logger(__name__)

    logger.info(f"Mode: {mode}")
    logger.info(f"  config_dir   = {config_dir}")
    logger.info(f"  sync_root    = {sync_root}")
    logger.info(f"  live_server  = {live_server}")
    logger.info(f"  www_dir      = {www_dir}")
    logger.info(f"  exclude_file = {exclude_file}")
    if mode == "client":
        logger.info(f"  multimc_base= {multimc_base}")
        logger.info(f"  instance_name= {instance_name}")

    # Build the client pack (staging)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            staging = Path(tmpdir)
            logger.info("Copying files to staging...")

            subdirs = ["mods", "config", "scripts", "kubejs", "config/ftbquests"]
            for sub in subdirs:
                (staging / sub).mkdir(parents=True, exist_ok=True)

            copy_directory_contents(sync_root / "client", staging / "mods", logger)
            copy_directory_contents(sync_root / "config", staging / "config", logger)
            copy_directory_contents(live_server / "kubejs", staging / "kubejs", logger)
            copy_directory_contents(live_server / "config" / "ftbquests",
                                    staging / "config" / "ftbquests", logger)

            # Read exclude patterns (once)
            exclude_patterns = get_exclude_patterns(exclude_file, logger)

            # --- Act based on mode ---
            if mode == "server":
                # Build ZIP
                date_str = datetime.datetime.now().strftime("%Y%m%d")
                zip_name = output_filename.format(date=date_str)
                output_zip = www_dir / zip_name
                logger.info(f"Creating zip: {output_zip}")
                create_zip_from_staging(staging, output_zip, exclude_patterns, logger)
                logger.info("Client pack created successfully.")
                logger.info(f"Location: {output_zip}")

            else:  # client mode
                if not instance_name:
                    raise ValueError("instance_name must be set in config for client mode")
                target_dir = multimc_base / instance_name / ".minecraft"
                logger.info(f"Deploying to client instance: {target_dir}")
                copy_with_exclusions(staging, target_dir, exclude_patterns, logger)
                logger.info("Client deployment completed successfully.")

    except Exception as e:
        logger.error(f"Failed to build client pack: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()