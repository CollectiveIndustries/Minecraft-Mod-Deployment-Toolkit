#!/usr/bin/env python3
"""
manifest_builder.py - Build a manifest.yaml from .jar files.
Scans directories, reads mods.toml metadata, and produces a single manifest.
Uses LoggingCore for logging, with --debug for verbose output.

Deduplication is enabled by default. With --curse, entries get source: curseforge
and a slug. With --resolve, the script queries CurseForge API using file
fingerprints to fill project_id and file_id (implies --curse).
"""

import argparse
import sys
import tomllib
import zipfile
import zlib
from pathlib import Path

import yaml

# Import collective-cores
from ConfigCore import ConfigManager
from CurseForgeAPy import CurseForgeAPI
from CurseForgeAPy.SchemaClasses import (
    ApiResponseCode,
    GetFingerprintMatchesRequestBody,
)
from LoggingCore import get_core, get_logger, setup_logging


def find_jars(directories, logger):
    """Yield all .jar files found under the given directories."""
    for d in directories:
        root = Path(d)
        if not root.is_dir():
            logger.warning(f"{root} is not a directory, skipping")
            continue
        yield from root.rglob("*.jar")


def extract_metadata(jar_path, logger):
    """Extract mod metadata from the jar's META-INF/mods.toml."""
    try:
        with zipfile.ZipFile(jar_path, 'r') as zf:
            toml_path = None
            for candidate in ["META-INF/mods.toml", "META-INF/neoforge.mods.toml"]:
                if candidate in zf.namelist():
                    toml_path = candidate
                    break
            if toml_path is None:
                logger.debug(f"No metadata file found in {jar_path}")
                return None

            with zf.open(toml_path) as f:
                data = tomllib.load(f)

            mods_list = data.get("mods", [])
            if not mods_list:
                logger.debug(f"No 'mods' list in {jar_path}")
                return None

            mod = mods_list[0]
            modid = mod.get("modId")
            version = mod.get("version")
            display_name = mod.get("displayName")
            side = mod.get("side", "BOTH").upper()

            depends = []
            deps = mod.get("dependencies", [])
            for dep in deps:
                if isinstance(dep, dict):
                    mod_id = dep.get("modId")
                    if mod_id:
                        depends.append(mod_id)
                elif isinstance(dep, str):
                    depends.append(dep)

            return {
                "id": modid,
                "version": version,
                "display_name": display_name,
                "side": side.lower(),
                "depends": depends,
            }
    except Exception as e:
        logger.error(f"Error reading {jar_path}: {e}")
        return None


def compute_fingerprint(jar_path):
    """Compute CRC32 fingerprint of a file (CurseForge uses this)."""
    crc = 0
    with open(jar_path, 'rb') as f:
        while chunk := f.read(8192):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def resolve_curse_ids(jars, logger, api_key):
    """
    Query CurseForge API to get project_id and file_id for each jar using search by slug and version.
    """
    client = CurseForgeAPI(api_key)
    results = {}

    for jar in jars:
        meta = extract_metadata(jar, logger)
        if not meta:
            continue

        slug = meta["id"].lower().replace("_", "-")
        version = meta["version"]
        # The gameId for Minecraft is always 432
        search_response = client.searchMods(gameId=432, searchFilter=slug, pageSize=5)

        if isinstance(search_response, ApiResponseCode):
            logger.debug(f"Search failed for {slug}: {search_response}")
            continue

        if not search_response.data:
            logger.debug(f"No mod found for slug {slug}")
            continue

        mod = search_response.data[0]
        files_response = client.getModFiles(mod.id)

        if isinstance(files_response, ApiResponseCode):
            logger.debug(f"Could not get files for {mod.id}: {files_response}")
            continue

        for file in files_response.data:
            if version in file.displayName or version in file.fileName:
                results[jar] = (mod.id, file.id)
                logger.debug(f"Resolved {meta['id']}: project={mod.id}, file={file.id}")
                break

    return results


def build_manifest(jars, default_side, logger, curse=False, resolve=False, api_key=None):
    """
    Build the manifest list from jar paths, deduplicating by mod id.
    Stores only the filename in the 'file' field.
    If curse is True, add source: curseforge and a slug.
    If resolve is True, also add project_id and file_id from CurseForge.
    """
    entries_with_jar = []
    for jar in jars:
        meta = extract_metadata(jar, logger)
        if meta is None:
            logger.debug(f"No metadata found in {jar}, skipping")
            continue

        side = meta["side"] if meta["side"] in ["client", "server", "both"] else default_side
        entry = {
            "id": meta["id"],
            "source": "local",
            "file": jar.name,
            "side": side,
            "enabled": True,
            "depends": meta["depends"],
            "conflicts": [],
            "tags": [],
            "version": meta["version"],
            "display_name": meta["display_name"],
        }
        if curse:
            entry["source"] = "curseforge"
            entry["slug"] = meta["id"].lower().replace("_", "-")
        entries_with_jar.append((entry, jar))

    # Deduplicate: keep first occurrence by mod id
    unique = {}
    for entry, jar in entries_with_jar:
        mid = entry["id"]
        if mid not in unique:
            unique[mid] = (entry, jar)

    # If resolve, get project/file IDs for the kept jars
    if resolve and api_key:
        kept_jars = [jar for _, jar in unique.values()]
        logger.info(f"Resolving CurseForge IDs for {len(kept_jars)} unique mods...")
        resolved = resolve_curse_ids(kept_jars, logger, api_key)
        resolved_count = 0
        for entry, jar in unique.values():
            if jar in resolved:
                project_id, file_id = resolved[jar]
                entry["project_id"] = project_id
                entry["file_id"] = file_id
                resolved_count += 1
                logger.debug(f"Resolved {entry['id']}: project_id={project_id}, file_id={file_id}")
            else:
                logger.warning(f"Could not resolve CurseForge IDs for {entry['file']}")
        logger.info(f"Resolved {resolved_count}/{len(unique)} mods")

    return sorted([entry for entry, _ in unique.values()], key=lambda x: x["id"].lower())


def main():
    parser = argparse.ArgumentParser(description="Build a manifest.yaml from .jar files.")
    parser.add_argument("--mods", action="append", required=True,
                        help="Directories to scan for .jar files (can be used multiple times)")
    parser.add_argument("--manifest", type=Path, default=Path("config.d/manifest.yaml"),
                        help="Output manifest file (default: config.d/manifest.yaml)")
    parser.add_argument("--side", default="both", choices=["client", "server", "both"],
                        help="Default side for mods that don't specify one (default: both)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--no-deduplicate", action="store_true",
                        help="Disable deduplication (keep duplicate entries)")
    parser.add_argument("--curse", action="store_true",
                        help="Configure entries for CurseForge: add slug and source: curseforge")
    parser.add_argument("--resolve", action="store_true",
                        help="Query CurseForge API to fill project_id and file_id (implies --curse)")
    parser.add_argument("--env-file", type=Path, default=None,
                        help="Path to .env file (default: checks ./.env and config.d/.env)")

    args = parser.parse_args()

    if args.resolve and not args.curse:
        args.curse = True

    # Setup logging
    log_config = {
        "color": True,
        "handlers": [
            {"type": "console", "color": True},
            {"type": "file", "path": "logs/manifest_builder.log",
             "max_bytes": 10_485_760, "backup_count": 5}
        ]
    }
    setup_logging(log_config)
    logger = get_logger(__name__)

    core = get_core()
    if core is not None:
        core.set_level(10 if args.debug else 20)

    # Load API key if resolving
    api_key = None
    if args.resolve:
        env_candidates = []
        if args.env_file:
            env_candidates.append(args.env_file)
        env_candidates.append(Path(".env"))
        env_candidates.append(Path("config.d/.env"))

        mgr = ConfigManager()
        loaded = False
        for env_file in env_candidates:
            if env_file.is_file():
                logger.debug(f"Loading .env from {env_file}")
                logger.debug(f"API key (first 4 chars): {api_key[:4] if api_key else 'None'}")
                logger.debug(f"API key length: {len(api_key) if api_key else 0}")
                mgr.file(env_file, format="env")
                loaded = True
                break
        if not loaded:
            logger.error("No .env file found. Checked: " + ", ".join(str(p) for p in env_candidates))
            sys.exit(1)

        config = mgr.load()
        logger.debug(f"Loaded config keys: {list(config._data.keys()) if hasattr(config, '_data') else config.as_dict().keys()}")
        logger.debug(f"CF_API_KEY value: {config.get('CF_API_KEY')}")
        api_key = config.get("CF_API_KEY") or config.get("api_key")
        if not api_key:
            logger.error("CF_API_KEY not found in .env; cannot resolve CurseForge IDs.")
            sys.exit(1)

    logger.info("Starting manifest builder")
    logger.info(f"  Scan directories: {args.mods}")
    logger.info(f"  Output manifest: {args.manifest}")
    logger.info(f"  Default side: {args.side}")
    logger.info(f"  Deduplicate: {not args.no_deduplicate}")
    logger.info(f"  CurseForge mode: {args.curse}")
    logger.info(f"  Resolve IDs: {args.resolve}")

    jars = list(find_jars(args.mods, logger))
    if not jars:
        logger.error("No .jar files found.")
        sys.exit(1)

    logger.info(f"Found {len(jars)} jar files.")

    manifest_entries = build_manifest(jars, args.side, logger,
                                      curse=args.curse,
                                      resolve=args.resolve,
                                      api_key=api_key)

    if not manifest_entries:
        logger.error("No valid mod metadata found.")
        sys.exit(1)

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.open('w') as f:
        yaml.dump({"mods": manifest_entries}, f, default_flow_style=False, sort_keys=False)

    logger.info(f"Manifest written to {args.manifest}")
    logger.info(f"Total mods: {len(manifest_entries)}")


if __name__ == "__main__":
    main()