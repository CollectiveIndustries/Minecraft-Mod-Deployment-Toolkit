#!/usr/bin/env python3
"""
deploy_pack.py - Generates a client ZIP archive (server mode) or deploys directly to a MultiMC instance.
Now supports --prism-index to use Prism .index folder directly for mod filtering.
"""
import argparse
import datetime
import os
import shutil
import sys
import tempfile
import traceback
from datetime import timezone
from pathlib import Path

from LoggingCore import get_logger, setup_logging

from .common import config as cfg
from .common import file_utils
from .common import manifest as manifest_utils
from .common import prism


def main():
    parser = argparse.ArgumentParser(description="Deploy client pack")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--server", action="store_true", help="Server mode: create ZIP (default)"
    )
    group.add_argument(
        "--client", action="store_true", help="Client mode: deploy to MultiMC instance"
    )
    parser.add_argument(
        "--config-dir",
        type=str,
        default=None,
        help="Path to configuration directory (default: config.d)",
    )
    parser.add_argument(
        "--prism-index",
        type=Path,
        help="Path to Prism .index folder (uses .pw.toml for side filtering, overrides manifest)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging and print full traceback on error",
    )
    args, remaining = parser.parse_known_args()

    mode = "client" if args.client else "server"
    target_side = "client" if mode == "client" else "server"

    config_dir = (
        Path(args.config_dir)
        if args.config_dir
        else Path(os.environ.get("DEPLOYPACK_CONFIG_DIR", "config.d"))
    )

    config = cfg.load_combined_config(
        config_dir=config_dir,
        base_name="deploy_pack",
        env_prefix="DEPLOYPACK",
        cli_args=remaining,
    )

    def get_path(key: str, default: str) -> Path:
        val = config.get(key)
        return Path(val) if val is not None else Path(default)

    sync_root = get_path("sync_root", "/home/minecraft/minecraft/sync")
    live_server = get_path("live_server", "/home/minecraft/minecraft/server")
    www_dir = get_path("www_dir", "/home/minecraft/minecraft/www")
    exclude_file = get_path("exclude_file", "/home/minecraft/nfs/sync/.rsync_exclude")
    output_filename = config.get("output_filename", "minecraft_client_{date}.zip")
    modpack_dir = get_path("modpack_dir", "./sync/downloads")

    manifest_path = config_dir / "manifest.yaml"

    multimc_base = config.get("multimc_base")
    if multimc_base is None:
        multimc_base = Path.home() / ".local/share/multimc/instances"
    else:
        multimc_base = Path(multimc_base)
    instance_name = config.get("instance_name")

    log_config = config.get("logging")
    if log_config is None:
        log_config = {
            "color": True,
            "handlers": [
                {"type": "console", "color": True},
                {
                    "type": "file",
                    "path": "logs/deploy_pack.log",
                    "max_bytes": 10_485_760,
                    "backup_count": 5,
                },
            ],
        }
    # Enable debug level if --debug
    if args.debug:
        log_config["level"] = "DEBUG"
        # Also set console and file to debug
        for handler in log_config.get("handlers", []):
            handler["level"] = "DEBUG"

    setup_logging(log_config)
    logger = get_logger(__name__)

    logger.info(f"Mode: {mode}")
    logger.info(f"  config_dir   = {config_dir}")
    logger.info(f"  sync_root    = {sync_root}")
    logger.info(f"  live_server  = {live_server}")
    logger.info(f"  www_dir      = {www_dir}")
    logger.info(f"  modpack_dir  = {modpack_dir}")
    logger.info(f"  exclude_file = {exclude_file}")
    if mode == "client":
        logger.info(f"  multimc_base= {multimc_base}")
        logger.info(f"  instance_name= {instance_name}")

    # Determine mod list source
    if args.prism_index:
        logger.info(f"Using Prism index: {args.prism_index}")
        if not args.prism_index.is_dir():
            logger.error(f"Prism index directory not found: {args.prism_index}")
            sys.exit(1)
        all_mods = prism.load_prism_index(args.prism_index)
        if not all_mods:
            logger.error("No mod entries found in Prism index.")
            sys.exit(1)
        logger.info(f"Loaded {len(all_mods)} mods from Prism index")
        side_mods = prism.filter_prism_entries_by_side(all_mods, target_side)
    else:
        # Fallback to manifest.yaml
        try:
            all_mods = manifest_utils.load_manifest(manifest_path)
            logger.info(f"Loaded {len(all_mods)} mods from manifest")
        except Exception as e:
            logger.error(f"Failed to load manifest: {e}")
            sys.exit(1)
        side_mods = manifest_utils.filter_mods_by_side(all_mods, target_side)

    logger.info(f"Filtered to {len(side_mods)} mods for side '{target_side}'")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            staging = Path(tmpdir)
            logger.info("Copying files to staging...")

            mods_dir = staging / "mods"
            mods_dir.mkdir(parents=True, exist_ok=True)

            # Copy mods
            copied_count = 0
            for entry in side_mods:
                if args.prism_index:
                    file_rel = entry.get("file")
                else:
                    file_rel = entry.get("file")
                if not file_rel:
                    logger.warning(f"Mod entry missing 'file' field, skipping")
                    continue
                src_file = modpack_dir / file_rel
                dest_file = mods_dir / file_rel
                if src_file.is_file():
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dest_file)
                    copied_count += 1
                    logger.debug(f"Copied mod: {file_rel}")
                else:
                    logger.warning(f"Mod file not found: {src_file} (expected for {entry.get('id')})")
            logger.info(f"Copied {copied_count}/{len(side_mods)} mod files")

            # Create subdirectories for config, scripts, etc.
            subdirs = ["config", "scripts", "kubejs", "config/ftbquests"]
            for sub in subdirs:
                (staging / sub).mkdir(parents=True, exist_ok=True)

            # Copy config, scripts, kubejs, ftbquests
            file_utils.copy_directory_contents(sync_root / "config", staging / "config", logger)
            scripts_src = sync_root / "scripts"
            if scripts_src.is_dir():
                file_utils.copy_directory_contents(scripts_src, staging / "scripts", logger)
            file_utils.copy_directory_contents(live_server / "kubejs", staging / "kubejs", logger)
            file_utils.copy_directory_contents(
                live_server / "config" / "ftbquests",
                staging / "config" / "ftbquests",
                logger,
            )

            exclude_patterns = file_utils.get_exclude_patterns(exclude_file, logger)

            if mode == "server":
                date_str = datetime.datetime.now(timezone.utc).strftime("%Y%m%d")
                zip_name = output_filename.format(date=date_str)
                output_zip = www_dir / zip_name
                logger.info(f"Creating zip: {output_zip}")
                file_utils.create_zip_from_staging(staging, output_zip, exclude_patterns, logger)
                logger.info(f"Client pack created successfully at {output_zip}")
            else:  # client
                if not instance_name:
                    raise ValueError("instance_name must be set in config for client mode")
                target_dir = multimc_base / instance_name / ".minecraft"
                logger.info(f"Deploying to client instance: {target_dir}")
                file_utils.copy_with_exclusions(staging, target_dir, exclude_patterns, logger)
                logger.info("Client deployment completed successfully.")

    except Exception as e:
        # Print full traceback to stderr for debugging
        if args.debug:
            traceback.print_exc()
        logger.exception(f"Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()