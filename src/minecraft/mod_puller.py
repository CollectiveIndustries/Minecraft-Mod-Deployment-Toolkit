#!/usr/bin/env python3
"""
mod_puller.py - Pull mods from CurseForge according to manifest.yaml,
placing each file exactly where the manifest says.
Uses ConfigCore and LoggingCore; API key from .env (loaded by ConfigCore).
"""

import sys
from pathlib import Path

import requests
import yaml

# Import collective-cores
from ConfigCore import ConfigManager
from CurseForgeAPy import CurseForgeAPI
from LoggingCore import get_logger, setup_logging

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def find_config_file(config_dir: Path) -> Path | None:
    """Return the first existing config file in config_dir."""
    for ext in [".toml", ".yaml", ".yml"]:
        candidate = config_dir / f"mod_puller{ext}"
        if candidate.is_file():
            return candidate
    return None


def load_manifest(path: Path):
    """Load YAML manifest from path."""
    if not path.is_file():
        raise FileNotFoundError(f"Manifest not found: {path}")
    with path.open() as f:
        data = yaml.safe_load(f)
    return data.get("mods", [])


def get_mod_file_url(client, slug, version, minecraft_version):
    """Return (download_url, filename) for the given slug and version."""
    # Search for mod
    results = client.searchMods(432, searchFilter=slug)  # 432 = Minecraft
    if not results.data:
        raise ValueError(f"Mod '{slug}' not found")
    mod = results.data[0]

    # Get files
    files = client.getModFiles(mod.id)
    for file in files.data:
        # Check version match (flexible)
        if version not in file.displayName and version not in file.fileName:
            continue
        # Check Minecraft version compatibility
        mc_versions = [gv.versionString for gv in file.gameVersions if gv.gameId == 432]
        if minecraft_version not in mc_versions:
            continue
        # Get download URL
        dl = client.getModFileDownloadUrl(mod.id, file.id)
        return dl.data.downloadUrl, file.fileName
    raise ValueError(f"No file found for '{slug}' version '{version}' on MC {minecraft_version}")


def download_file(url, output_path):
    """Download file from url to output_path."""
    response = requests.get(url, stream=True)
    response.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'wb') as f:
        f.writelines(response.iter_content(chunk_size=8192))


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    # 1. Locate config
    config_dir = Path("config.d")
    config_path = find_config_file(config_dir)
    if config_path is None:
        print(f"WARNING: No config file found in {config_dir}. Using defaults.", file=sys.stderr)

    # 2. Build ConfigManager
    mgr = ConfigManager()

    # Load .env file first (if exists) - this provides CF_API_KEY
    env_file = Path(".env")
    if env_file.is_file():
        mgr.file(env_file)   # ConfigCore parses .env syntax

    # Load the main config file (TOML/YAML)
    if config_path is not None:
        mgr.file(config_path)

    # Override with system environment variables (prefix MODPULLER_)
    mgr.env("MODPULLER")

    # Load config
    config = mgr.load()

    # 3. Extract required settings
    api_key = config.get("CF_API_KEY") or config.get("api_key")
    if not api_key:
        print("ERROR: CF_API_KEY not found in .env or config. Aborting.", file=sys.stderr)
        sys.exit(1)

    output_root = Path(config.get("output_root", "./modpack"))
    manifest_file = config_dir / config.get("manifest_file", "manifest.yaml")
    minecraft_version = config.get("minecraft_version", "1.20.1")

    # 4. Setup logging
    log_config = config.get("logging")
    if log_config is None:
        log_config = {
            "color": True,
            "handlers": [
                {"type": "console", "color": True},
                {"type": "file",
                 "path": "logs/mod_puller.log",
                 "max_bytes": 10_485_760,
                 "backup_count": 5}
            ]
        }
    setup_logging(log_config)
    logger = get_logger(__name__)

    logger.info("Starting mod puller")
    logger.info(f"  Output root: {output_root}")
    logger.info(f"  Manifest: {manifest_file}")
    logger.info(f"  Minecraft version: {minecraft_version}")

    # 5. Load manifest
    try:
        mods = load_manifest(manifest_file)
    except Exception as e:
        logger.error(f"Failed to load manifest: {e}")
        sys.exit(1)

    logger.info(f"Found {len(mods)} mod entries")

    # 6. Initialize CurseForge client
    client = CurseForgeAPI(api_key)

    # 7. Process each mod
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
            # Local file - ensure it exists (or copy it, but assume it's already there)
            if target_path.is_file():
                logger.info(f"Local file exists: {target_path}")
            else:
                logger.warning(f"Local file not found: {target_path} - you may need to place it manually")
            continue

        elif source == "curseforge":
            slug = entry.get("slug")
            version = entry.get("version")
            if not slug or not version:
                logger.warning(f"Mod {mod_id} missing slug/version, skipping")
                continue
            try:
                logger.info(f"Processing {mod_id} ({slug}) version {version}...")
                download_url, _filename = get_mod_file_url(client, slug, version, minecraft_version)
                if target_path.exists():
                    logger.info(f"  File already exists: {target_path}")
                    continue
                logger.info(f"  Downloading from {download_url}")
                download_file(download_url, target_path)
                logger.info(f"  Downloaded to {target_path}")
            except Exception as e:
                logger.error(f"Failed to process {mod_id}: {e}")
        else:
            logger.warning(f"Unknown source '{source}' for mod {mod_id}, skipping")

    logger.info("Mod puller finished.")


if __name__ == "__main__":
    main()