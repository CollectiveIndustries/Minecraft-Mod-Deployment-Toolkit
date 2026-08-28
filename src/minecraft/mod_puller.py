"""
mod_puller.py - Pull mods from CurseForge according to manifest.yaml,
placing each file exactly where the manifest says.
"""

import os
import sys
from pathlib import Path

from LoggingCore import get_logger, setup_logging

from .common import config as cfg
from .common import curseforge as cf
from .common import file_utils
from .common import manifest as manifest_utils


def main():
    config_dir = Path("config.d")

    config = cfg.load_combined_config(
        config_dir=config_dir,
        base_name="mod_puller",
        env_prefix="MODPULLER",
        env_file_candidates=[config_dir / ".env", Path(".env")],
    )

    api_key = (
        config.get("CF_API_KEY") or config.get("api_key") or os.getenv("CF_API_KEY")
    )
    if not api_key:
        print(
            "ERROR: CF_API_KEY not found in .env, config, or environment. Aborting.",
            file=sys.stderr,
        )
        sys.exit(1)

    output_root = Path(config.get("output_root", "./modpack"))
    manifest_file = config_dir / config.get("manifest_file", "manifest.yaml")
    minecraft_version = config.get("minecraft_version", "1.20.1")

    log_config = config.get("logging")
    if log_config is None:
        log_config = {
            "color": True,
            "handlers": [
                {"type": "console", "color": True},
                {
                    "type": "file",
                    "path": "logs/mod_puller.log",
                    "max_bytes": 10_485_760,
                    "backup_count": 5,
                },
            ],
        }
    setup_logging(log_config)
    logger = get_logger(__name__)

    logger.info("Starting mod puller")
    logger.info(f"  Output root: {output_root}")
    logger.info(f"  Manifest: {manifest_file}")
    logger.info(f"  Minecraft version: {minecraft_version}")

    try:
        mods = manifest_utils.load_manifest(manifest_file)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to load manifest: {e}")
        sys.exit(1)

    logger.info(f"Found {len(mods)} mod entries")

    for entry in mods:
        mod_id = entry.get("id")
        source = entry.get("source", "local")
        file_rel = entry.get("file")
        enabled = entry.get("enabled", True)

        if not enabled:
            logger.info(f"Skipping disabled mod: {mod_id}")
            continue
        if not file_rel:
            logger.warning(f"Mod {mod_id} has no 'file' field, skipping")
            continue

        target_path = output_root / file_rel

        if source == "local":
            if target_path.is_file():
                logger.info(f"Local file exists: {target_path}")
            else:
                logger.warning(
                    f"Local file not found: {target_path} - you may need to place it manually"
                )
            continue

        elif source == "curseforge":
            project_id = entry.get("project_id")
            file_id = entry.get("file_id")
            if project_id and file_id:
                try:
                    logger.info(
                        f"Processing {mod_id} using project_id={project_id}, file_id={file_id}..."
                    )
                    download_url = cf.get_download_url_by_ids(
                        project_id, file_id, api_key
                    )
                    if target_path.exists():
                        logger.info(f"  File already exists: {target_path}")
                        continue
                    logger.info(f"  Downloading from {download_url}")
                    file_utils.download_file(download_url, target_path)
                    logger.info(f"  Downloaded to {target_path}")
                    continue
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Direct download failed for {mod_id}: {e}")

            slug = entry.get("slug")
            version = entry.get("version")
            if not slug or not version:
                logger.warning(f"Mod {mod_id} missing slug/version, skipping")
                continue
            try:
                logger.info(
                    f"Processing {mod_id} ({slug}) version {version} via search..."
                )
                download_url, _filename = cf.get_mod_file_url_by_slug(
                    slug, version, minecraft_version, api_key
                )
                if target_path.exists():
                    logger.info(f"  File already exists: {target_path}")
                    continue
                logger.info(f"  Downloading from {download_url}")
                file_utils.download_file(download_url, target_path)
                logger.info(f"  Downloaded to {target_path}")
            except Exception:
                logger.exception(f"Failed to process {mod_id}")
        else:
            logger.warning(f"Unknown source '{source}' for mod {mod_id}, skipping")

    logger.info("Mod puller finished.")


if __name__ == "__main__":
    main()
