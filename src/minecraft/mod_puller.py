#!/usr/bin/env python3
"""
mod_puller.py - Pull mods from CurseForge according to manifest.yaml,
placing each file exactly where the manifest says.
Uses ConfigCore and LoggingCore; API key from .env (loaded by python-dotenv).
Supports direct download via project_id/file_id, with slug/version fallback.
"""

import os
import sys
from pathlib import Path

import requests
import yaml

# Import collective-cores
from ConfigCore import ConfigManager
from dotenv import load_dotenv
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


def curseforge_request(endpoint, api_key, method="GET", params=None, json_data=None):
    """Make a request to the CurseForge API with proper headers."""
    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
    }
    if json_data is not None:
        headers["Content-Type"] = "application/json"
    url = f"https://api.curseforge.com{endpoint}"
    resp = requests.request(
        method, url, headers=headers, params=params, json=json_data, timeout=30
    )
    resp.raise_for_status()
    return resp.json()


def get_download_url_by_ids(project_id: int, file_id: int, api_key: str) -> str:
    """Get download URL directly using project_id and file_id."""
    resp = curseforge_request(
        f"/v1/mods/{project_id}/files/{file_id}/download-url", api_key
    )
    return resp["data"]


def get_mod_file_url_by_slug(
    slug: str, version: str, minecraft_version: str, api_key: str
):
    """
    Fallback: search by slug, fetch files, find matching file.
    Uses raw API calls (not wrapper).
    """
    # 1. Search by slug
    search_resp = curseforge_request(
        "/v1/mods/search", api_key, params={"gameId": 432, "slug": slug, "pageSize": 1}
    )
    data = search_resp.get("data", [])
    if not data:
        # Try searchFilter as last resort
        search_resp = curseforge_request(
            "/v1/mods/search",
            api_key,
            params={"gameId": 432, "searchFilter": slug, "pageSize": 1},
        )
        data = search_resp.get("data", [])
        if not data:
            raise ValueError(f"Mod with slug '{slug}' not found")
    mod = data[0]
    mod_id = mod["id"]

    # 2. Get all files (paginated)
    all_files = []
    page_size = 100
    index = 0
    max_pages = 5
    for _ in range(max_pages):
        try:
            resp = curseforge_request(
                f"/v1/mods/{mod_id}/files",
                api_key,
                params={"pageSize": page_size, "index": index},
            )
            page = resp.get("data", [])
            if not page:
                break
            all_files.extend(page)
            pagination = resp.get("pagination", {})
            total = pagination.get("totalCount", 0)
            if index + page_size >= total:
                break
            index += page_size
        except Exception:
            break

    if not all_files:
        raise ValueError(f"No files found for mod '{slug}'")

    # 3. Filter by Minecraft version (1.20.1)
    filtered = [
        f
        for f in all_files
        if any("1.20.1" in gv or "1.20" in gv for gv in f.get("gameVersions", []))
    ]
    if not filtered:
        filtered = all_files  # fallback to all

    # 4. Try to match by version string in fileName or displayName
    matched = None
    for f in filtered:
        fname = f.get("fileName", "")
        display = f.get("displayName", "")
        if version in fname or version in display:
            matched = f
            break

    # 5. If not found, try exact filename match (case-insensitive)
    if not matched:
        jar_name = f"{slug}.jar"  # approximate
        for f in filtered:
            if f.get("fileName", "").lower() == jar_name.lower():
                matched = f
                break

    # 6. Fallback to latest file (if any)
    if not matched and filtered:
        sorted_files = sorted(
            filtered, key=lambda f: f.get("fileDate", ""), reverse=True
        )
        matched = sorted_files[0]

    if not matched:
        raise ValueError(
            f"No matching file for '{slug}' version '{version}' on MC {minecraft_version}"
        )

    # 7. Get download URL
    download_url = get_download_url_by_ids(mod_id, matched["id"], api_key)
    return download_url, matched["fileName"]


def download_file(url, output_path):
    """Download file from url to output_path."""
    response = requests.get(url, stream=True, timeout=30)
    response.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.writelines(response.iter_content(chunk_size=8192))


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------


def main():
    # 1. Locate config
    config_dir = Path("config.d")
    config_path = find_config_file(config_dir)
    if config_path is None:
        print(
            f"WARNING: No config file found in {config_dir}. Using defaults.",
            file=sys.stderr,
        )

    # 2. Build ConfigManager
    mgr = ConfigManager()

    # Load .env files using python-dotenv (they will be added to os.environ)
    env_loaded = False
    env_candidates = [config_dir / ".env", Path(".env")]
    for env_file in env_candidates:
        if env_file.is_file():
            load_dotenv(dotenv_path=env_file)
            env_loaded = True
            break
    if not env_loaded:
        print(
            "WARNING: No .env file found. Checked: "
            + ", ".join(str(p) for p in env_candidates),
            file=sys.stderr,
        )

    # Load the main config file (TOML/YAML)
    if config_path is not None:
        mgr.file(config_path)

    # Override with system environment variables (prefix MODPULLER_)
    mgr.env("MODPULLER")

    # Load config
    config = mgr.load()

    # 3. Extract required settings
    # First try config (from file or MODPULLER_ prefixed env), then raw environment variable CF_API_KEY
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

    # 4. Setup logging
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

    # 5. Load manifest
    try:
        mods = load_manifest(manifest_file)
    except Exception as e:
        logger.error(f"Failed to load manifest: {e}")
        sys.exit(1)

    logger.info(f"Found {len(mods)} mod entries")

    # 6. Process each mod
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
                logger.warning(
                    f"Local file not found: {target_path} - you may need to place it manually"
                )
            continue

        elif source == "curseforge":
            # Try to use project_id and file_id first (fast and reliable)
            project_id = entry.get("project_id")
            file_id = entry.get("file_id")
            if project_id and file_id:
                try:
                    logger.info(
                        f"Processing {mod_id} using project_id={project_id}, file_id={file_id}..."
                    )
                    download_url = get_download_url_by_ids(project_id, file_id, api_key)
                    if target_path.exists():
                        logger.info(f"  File already exists: {target_path}")
                        continue
                    logger.info(f"  Downloading from {download_url}")
                    download_file(download_url, target_path)
                    logger.info(f"  Downloaded to {target_path}")
                    continue
                except Exception as e:
                    logger.warning(
                        f"Direct download failed for {mod_id} (project_id={project_id}, file_id={file_id}): {e}"
                    )
                    # Fall through to slug/version method

            # Fallback: use slug and version
            slug = entry.get("slug")
            version = entry.get("version")
            if not slug or not version:
                logger.warning(f"Mod {mod_id} missing slug/version, skipping")
                continue
            try:
                logger.info(
                    f"Processing {mod_id} ({slug}) version {version} via search..."
                )
                download_url, filename = get_mod_file_url_by_slug(
                    slug, version, minecraft_version, api_key
                )
                # Note: filename might differ from file_rel; we keep target_path as specified in manifest
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
