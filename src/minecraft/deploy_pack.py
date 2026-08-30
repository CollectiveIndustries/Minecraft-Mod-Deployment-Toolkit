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


def load_mod_list(
    prism_index: Path,
    config_dir: Path,
    target_side: str,
    logger,
) -> list:
    """
    Load mod entries from a Prism index directory.
    Returns a list of mod dicts filtered by target side.
    """
    if not prism_index.is_dir():
        raise ValueError(f"Prism index directory not found: {prism_index}")
    all_mods = prism.load_prism_index(prism_index)
    if not all_mods:
        raise ValueError("No mod entries found in Prism index.")
    logger.info(f"Loaded {len(all_mods)} mods from Prism index")

    override_path = config_dir / "side_overrides.toml"
    overrides_data = overrides.load_side_overrides(override_path)
    if overrides_data:
        logger.info(f"Loaded {len(overrides_data)} side overrides")
        all_mods = overrides.apply_side_overrides(all_mods, overrides_data)

    side_mods = prism.filter_prism_entries_by_side(all_mods, target_side)
    logger.info(f"Filtered to {len(side_mods)} mods for side '{target_side}'")
    return side_mods


def prepare_staging(
    side_mods: list,
    modpack_dir: Path,
    sync_root: Path,
    live_server: Path,
    logger,
) -> Path:
    """
    Create a staging directory with all mods, configs, scripts, kubejs, and ftbquests.
    Returns the Path to the staging directory.
    """
    staging = Path(tempfile.mkdtemp(prefix="deploy_staging_"))
    logger.info(f"Staging directory: {staging}")

    # Copy mods
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

        # Ensure file exists and hash matches (download if needed)
        if not file_utils.ensure_mod_file(
            src,
            entry.get("download_url"),
            entry.get("hash_value"),
            entry.get("hash_format", "sha512"),
            logger,
        ):
            logger.warning(f"Skipping mod {file_rel} due to missing/corrupt file")
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
        logger.debug(f"Copied mod: {file_rel}")

    logger.info(f"Copied {copied}/{len(side_mods)} mod files")

    # Prepare subdirs
    for sub in ["config", "scripts", "kubejs", "config/ftbquests"]:
        (staging / sub).mkdir(parents=True, exist_ok=True)

    # Copy configs, scripts, kubejs, ftbquests
    file_utils.copy_directory_contents(sync_root / "config", staging / "config", logger)
    scripts_src = sync_root / "scripts"
    if scripts_src.is_dir():
        file_utils.copy_directory_contents(scripts_src, staging / "scripts", logger)
    file_utils.copy_directory_contents(
        live_server / "kubejs", staging / "kubejs", logger
    )
    file_utils.copy_directory_contents(
        live_server / "config" / "ftbquests",
        staging / "config" / "ftbquests",
        logger,
    )

    return staging


def create_client_zip(
    staging_dir: Path,
    www_dir: Path,
    filename_template: str,
    exclude_patterns: list,
    logger,
) -> Path:
    """Create a ZIP archive from the staging directory and return its path."""
    date_str = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")
    zip_name = filename_template.format(date=date_str)
    output_zip = www_dir / zip_name
    logger.info(f"Creating zip: {output_zip}")
    file_utils.create_zip_from_staging(
        staging_dir, output_zip, exclude_patterns, logger
    )
    logger.info(f"Client pack created successfully at {output_zip}")
    return output_zip


def deploy_to_server(
    staging_dir: Path,
    live_server: Path,
    exclude_patterns: list,
    logger,
):
    """Copy staging contents to the live server directory (with cleanup)."""
    logger.info(f"Deploying to live_server: {live_server}")
    file_utils.copy_with_exclusions(
        staging_dir, live_server, exclude_patterns, logger, clean=True
    )
    logger.info("Live server updated successfully (cleaned).")


def deploy_to_client(
    staging_dir: Path,
    multimc_base: Path,
    instance_name: str,
    exclude_patterns: list,
    logger,
):
    """Deploy staging contents to a MultiMC client instance."""
    target_dir = multimc_base / instance_name / ".minecraft"
    logger.info(f"Deploying to client instance: {target_dir}")
    file_utils.copy_with_exclusions(staging_dir, target_dir, exclude_patterns, logger)
    logger.info("Client deployment completed.")


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

    # Config directory resolution
    if args.config_dir:
        config_dir = Path(args.config_dir)
        if config_dir.is_file():
            config_dir = config_dir.parent
    else:
        config_dir = Path(os.environ.get("DEPLOYPACK_CONFIG_DIR", "config.d"))

    # Load configuration
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

    # Prism index is always located inside modpack_dir
    prism_index_dir = modpack_dir / ".index"

    multimc_base = config.get(
        "multimc_base", str(Path.home() / ".local/share/multimc/instances")
    )
    instance_name = config.get("instance_name")

    # Logging setup
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
    logger.info(f"  prism_index  = {prism_index_dir}")
    logger.info(f"  exclude_file = {exclude_file}")
    if mode == "client":
        logger.info(f"  multimc_base= {multimc_base}")
        logger.info(f"  instance_name= {instance_name}")

    # Load mod list (Prism index required)
    try:
        side_mods = load_mod_list(
            prism_index_dir,
            config_dir,
            target_side,
            logger,
        )
    except (ValueError, OSError, FileNotFoundError) as e:
        logger.error(f"Failed to load mods: {e}")
        sys.exit(1)

    # Build staging
    try:
        staging = prepare_staging(
            side_mods,
            modpack_dir,
            sync_root,
            live_server,
            logger,
        )
        exclude_patterns = file_utils.get_exclude_patterns(exclude_file, logger)

        if mode == "server":
            create_client_zip(
                staging,
                www_dir,
                output_filename,
                exclude_patterns,
                logger,
            )
            if not args.no_deploy:
                deploy_to_server(
                    staging,
                    live_server,
                    exclude_patterns,
                    logger,
                )
            else:
                logger.info("Skipping live_server deployment (--no-deploy).")
        else:  # client mode
            if not instance_name:
                logger.error("instance_name must be set for client mode")
                sys.exit(1)
            deploy_to_client(
                staging,
                Path(multimc_base),
                instance_name,
                exclude_patterns,
                logger,
            )

    except Exception:
        if args.debug:
            traceback.print_exc()
        logger.exception("Deployment failed")
        sys.exit(1)
    finally:
        if "staging" in locals() and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
            logger.debug(f"Cleaned up staging: {staging}")


if __name__ == "__main__":
    main()
