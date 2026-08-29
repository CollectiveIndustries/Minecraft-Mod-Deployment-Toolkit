"""
deploy_pack.py - Generate server ZIP and update live server.
Uses Prism .index for mod metadata and ConfigCore for configuration.
"""

import argparse
import datetime
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from LoggingCore import get_logger, setup_logging

from .common import config as cfg
from .common import file_utils, overrides, prism
from .common import manifest as manifest_utils


def main():
    parser = argparse.ArgumentParser(description="Deploy client pack")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--server",
        action="store_true",
        help="Server mode: create ZIP and update live_server (default)",
    )
    group.add_argument(
        "--client", action="store_true", help="Client mode: deploy to MultiMC instance"
    )
    parser.add_argument(
        "--config-dir",
        type=str,
        default=None,
        help="Path to config directory (default: config.d). If a file is given, its parent is used.",
    )
    parser.add_argument(
        "--prism-index",
        type=Path,
        help="Path to Prism .index folder (uses .pw.toml for side filtering)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging and print full traceback",
    )
    parser.add_argument(
        "--no-deploy",
        action="store_true",
        help="When used with --server, skip copying to live_server (only create ZIP).",
    )
    args, remaining = parser.parse_known_args()

    mode = "client" if args.client else "server"
    target_side = "client" if mode == "client" else "server"

    # --- Config directory ---
    if args.config_dir:
        config_dir = Path(args.config_dir)
        if config_dir.is_file():
            config_dir = config_dir.parent
    else:
        config_dir = Path(os.environ.get("DEPLOYPACK_CONFIG_DIR", "config.d"))

    # --- Load configuration with ConfigCore ---
    config = cfg.load_config(
        config_dir=config_dir,
        base_name="deploy_pack",
        env_prefix="DEPLOYPACK",
        cli_args=remaining,
        env_file=config_dir / ".env",
    )

    if args.debug:
        print("=== Loaded configuration ===")
        for key, value in config.as_dict().items():
            print(f"{key} = {value}")
        print("============================")

    def get_path(key: str, default: str) -> Path:
        val = config.get(key)
        return Path(val) if val is not None else Path(default)

    sync_root = get_path("sync_root", "./sync")
    live_server = get_path("live_server", "./server")
    www_dir = get_path("www_dir", "./www")
    exclude_file = get_path("exclude_file", "./sync/.rsync_exclude")
    output_filename = config.get("output_filename", "minecraft_client_{date}.zip")
    modpack_dir = get_path("modpack_dir", "./sync/downloads")

    manifest_path = config_dir / "manifest.yaml"
    multimc_base = config.get(
        "multimc_base", str(Path.home() / ".local/share/multimc/instances")
    )
    instance_name = config.get("instance_name")

    # --- Logging setup ---
    log_config = config.get("logging")
    if not log_config:
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
    log_config["level"] = "DEBUG" if args.debug else "INFO"
    for h in log_config.get("handlers", []):
        h["level"] = log_config["level"]

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

    # --- Determine mod list ---
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

        # Apply side overrides
        override_path = config_dir / "side_overrides.toml"
        overrides_data = overrides.load_side_overrides(override_path)
        if overrides_data:
            logger.info(f"Loaded {len(overrides_data)} side overrides")
            all_mods = overrides.apply_side_overrides(all_mods, overrides_data)
        else:
            logger.debug("No side overrides found.")

        side_mods = prism.filter_prism_entries_by_side(all_mods, target_side)
    else:
        try:
            all_mods = manifest_utils.load_manifest(manifest_path)
            logger.info(f"Loaded {len(all_mods)} mods from manifest")
            side_mods = manifest_utils.filter_mods_by_side(all_mods, target_side)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to load manifest: {e}")
            sys.exit(1)

    logger.info(f"Filtered to {len(side_mods)} mods for side '{target_side}'")

    # --- Build staging ---
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            staging = Path(tmpdir)
            logger.info("Copying files to staging...")

            mods_dir = staging / "mods"
            mods_dir.mkdir(parents=True, exist_ok=True)

            copied = 0
            for entry in side_mods:
                file_rel = entry.get("file")
                if not file_rel:
                    logger.warning("Mod entry missing 'file' field, skipping")
                    continue
                src = modpack_dir / file_rel
                dst = mods_dir / file_rel
                if src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1
                    logger.debug(f"Copied mod: {file_rel}")
                else:
                    logger.warning(f"Mod file not found: {src}")
            logger.info(f"Copied {copied}/{len(side_mods)} mod files")

            # Prepare subdirs
            for sub in ["config", "scripts", "kubejs", "config/ftbquests"]:
                (staging / sub).mkdir(parents=True, exist_ok=True)

            # Copy configs, scripts, kubejs, ftbquests
            file_utils.copy_directory_contents(
                sync_root / "config", staging / "config", logger
            )
            scripts_src = sync_root / "scripts"
            if scripts_src.is_dir():
                file_utils.copy_directory_contents(
                    scripts_src, staging / "scripts", logger
                )
            file_utils.copy_directory_contents(
                live_server / "kubejs", staging / "kubejs", logger
            )
            file_utils.copy_directory_contents(
                live_server / "config" / "ftbquests",
                staging / "config" / "ftbquests",
                logger,
            )

            exclude_patterns = file_utils.get_exclude_patterns(exclude_file, logger)

            if mode == "server":
                # Create ZIP
                date_str = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")
                zip_name = output_filename.format(date=date_str)
                output_zip = www_dir / zip_name
                logger.info(f"Creating zip: {output_zip}")
                file_utils.create_zip_from_staging(
                    staging, output_zip, exclude_patterns, logger
                )
                logger.info(f"Client pack created successfully at {output_zip}")

                # Update live server (with clean sync)
                if not args.no_deploy:
                    logger.info(f"Deploying to live_server: {live_server}")
                    file_utils.copy_with_exclusions(
                        staging, live_server, exclude_patterns, logger, clean=True
                    )
                    logger.info("Live server updated successfully (cleaned).")
                else:
                    logger.info("Skipping live_server deployment (--no-deploy).")

            else:  # client mode
                if not instance_name:
                    raise ValueError("instance_name must be set for client mode")
                target_dir = Path(multimc_base) / instance_name / ".minecraft"
                logger.info(f"Deploying to client instance: {target_dir}")
                file_utils.copy_with_exclusions(
                    staging, target_dir, exclude_patterns, logger
                )
                logger.info("Client deployment completed.")

    except Exception:
        if args.debug:
            traceback.print_exc()
        logger.exception("Failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
