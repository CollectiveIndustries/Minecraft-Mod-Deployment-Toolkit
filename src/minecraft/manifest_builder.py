#!/usr/bin/env python3
"""
manifest_builder.py - Build a manifest.yaml from .jar files.
Scans directories, reads mods.toml metadata, and produces a single manifest.
Uses LoggingCore for logging, with --debug for verbose output.

Deduplication is enabled by default. With --curse, entries get source: curseforge
and a slug. With --resolve, the script queries CurseForge API to fill project_id
and file_id (implies --curse).
"""

import argparse
import sys
import tomllib
import zipfile
import zlib
import re
from pathlib import Path
from typing import Dict, Tuple, Optional, List

import requests
import yaml

# Import collective-cores
from ConfigCore import ConfigManager
from LoggingCore import get_core, get_logger, setup_logging


# ----------------------------------------------------------------------
# Slug correction mapping: metadata modid -> correct CurseForge slug
# Add entries for mods where the metadata's modId is not the CF slug.
# ----------------------------------------------------------------------
SLUG_CORRECTIONS = {
    # FTB mods
    "ftbessentials": "ftb-essentials",
    "ftblibrary": "ftb-library",
    "ftbteams": "ftb-teams-forge",           # corrected
    "ftbquests": "ftb-quests-forge",         # corrected
    "ftbchunks": "ftb-chunks-forge",         # corrected
    "ftbfiltersystem": "ftb-filter-system",
    "ftbxmodcompat": "ftb-xmod-compat",

    # Compass mods
    "naturescompass": "natures-compass",
    "explorerscompass": "explorers-compass",

    # Essentials
    "inventoryessentials": "inventory-essentials",

    # Sophisticated
    "sophisticatedcore": "sophisticated-core",
    "sophisticatedbackpacks": "sophisticated-backpacks",

    # Libs
    "resourcefullib": "resourceful-lib",
    "forgeconfigscreens": "config-menus-forge",   # corrected
    "puzzleslib": "puzzles-lib",
    "anvianslib": "anvians-lib",

    # Create addons & related
    "createaddition": "create-addition",
    "create-new-age": "create-new-age",
    "create-hypertube": "hypertubes",             # corrected
    "create-unbreakable": "create-unbreakable-tools",  # corrected
    "create-decoration": "create-deco",           # corrected
    "create-ultimate-factory": "create-ultimate-factory",
    "create": "create",                           # already correct

    # JEI / JEP
    "jeimultiblocks": "jei-multiblocks",
    "justenoughprofessions": "just-enough-professions-jep",  # corrected

    # Storage
    "storagedrawers": "storage-drawers",
    "storagedrawersextra": "storage-drawers-extra",

    # Villages/underground
    "underground-village": "underground-villages-stoneholm",  # corrected

    # Misc
    "morerelics": "more-relics",
    "bits-n-bobs": "create-bits-n-bobs",          # corrected
    "ceilingtorch": "ceiling-torch",
    "lighty": "lighty",
    "collective": "collective",
    "enderchests": "ender-chests",
    "endertanks": "ender-tanks",
    "carryon": "carry-on",
    "biomesoplenty": "biomes-o-plenty",
    "morered": "more-red",
    "moreredxcctcompat": "more-red-cct-compat",
    "shetiphiancore": "shetiphian-core",
    "quarry": "quarry",
    "worldedit": "world-edit",
    "invTweaks": "inv-tweaks",
    "craftingtweaks": "crafting-tweaks",
    "applied-kjs": "applied-kjs",
    "ae2": "applied-energistics-2",
    "jadeaddons": "jade-addons",
    "createsweetsandtreets": "create-sweets-and-treats",
    "svmm": "server-side-vein-miner",

    # Additions from latest corrections
    "mininggadgets": "mining-gadgets",
    "inventory-sorter": "inventory-sorter",
    "enchanting-infuser": "enchanting-infuser",
    "small-ships": "small-ships",
    "architectury": "architectury-api",
    "immersive-engineering": "immersive-engineering",
}


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


def curseforge_request(endpoint, api_key, method="GET", params=None, json_data=None):
    """Make a request to the CurseForge API with proper headers."""
    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
    }
    if json_data is not None:
        headers["Content-Type"] = "application/json"
    url = f"https://api.curseforge.com{endpoint}"
    resp = requests.request(method, url, headers=headers, params=params, json=json_data, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_all_files(mod_id, api_key, logger):
    """Fetch all files for a mod, handling pagination."""
    all_files = []
    page_size = 100
    index = 0
    max_pages = 5  # up to 500 files, enough for most mods
    for _ in range(max_pages):
        try:
            resp = curseforge_request(
                f"/v1/mods/{mod_id}/files",
                api_key,
                params={"pageSize": page_size, "index": index}
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
        except Exception as e:
            logger.debug(f"Could not fetch files page {index//page_size}: {e}")
            break
    return all_files


def find_mod_by_slug(slug, api_key, logger):
    """Find a mod by its exact slug. Returns mod dict or None."""
    try:
        resp = curseforge_request(
            "/v1/mods/search",
            api_key,
            params={"gameId": 432, "slug": slug, "pageSize": 1}
        )
        data = resp.get("data", [])
        if data:
            return data[0]
    except Exception as e:
        logger.debug(f"Slug search failed for {slug}: {e}")
    return None


def resolve_by_search_direct(jars, logger, api_key):
    """
    Resolve each jar:
      1. Use corrected slug (if mapping exists) or original.
      2. Search by slug (exact match).
      3. If fails, try display_name as last resort (but with sanity check).
      4. Fetch all files, filter by MC version 1.20.1.
      5. Match file by exact filename, version from filename, or metadata version.
      6. If still no match, pick the latest file (fallback).
    """
    results = {}

    for jar in jars:
        meta = extract_metadata(jar, logger)
        if not meta:
            continue

        original_slug = meta["id"].lower().replace("_", "-")
        # Apply correction
        corrected_slug = SLUG_CORRECTIONS.get(original_slug, original_slug)
        display_name = meta.get("display_name")
        version_from_meta = meta.get("version")
        jar_name = jar.name

        # ----- Find the mod using corrected slug -----
        mod = None

        # 1. Try corrected slug
        if corrected_slug != original_slug:
            mod = find_mod_by_slug(corrected_slug, api_key, logger)
            if mod:
                logger.debug(f"Found mod by corrected slug '{corrected_slug}': {mod['name']} (id={mod['id']}) for {jar_name}")

        # 2. Try original slug
        if not mod:
            mod = find_mod_by_slug(original_slug, api_key, logger)
            if mod:
                logger.debug(f"Found mod by original slug '{original_slug}': {mod['name']} (id={mod['id']}) for {jar_name}")

        # 3. Last resort: searchFilter with display name (with sanity check)
        if not mod and display_name:
            try:
                resp = curseforge_request(
                    "/v1/mods/search",
                    api_key,
                    params={"gameId": 432, "searchFilter": display_name, "pageSize": 5}
                )
                candidates = resp.get("data", [])
                if candidates:
                    # Prefer candidates whose slug contains the original slug
                    for cand in candidates:
                        cand_slug = cand.get("slug", "")
                        if original_slug in cand_slug or corrected_slug in cand_slug:
                            mod = cand
                            break
                    if not mod:
                        # Pick the first one that has "forge" in the name? or just first
                        mod = candidates[0]
                    logger.debug(f"Found mod by display_name: {mod['name']} (id={mod['id']}) for {jar_name}")
            except Exception as e:
                logger.debug(f"Display_name search failed: {e}")

        if not mod:
            logger.debug(f"No mod found for {jar_name} (slugs: {original_slug}/{corrected_slug})")
            continue

        mod_id = mod["id"]

        # ----- Get all files for this mod -----
        all_files = fetch_all_files(mod_id, api_key, logger)
        if not all_files:
            logger.debug(f"No files found for mod {mod_id} (slug {original_slug})")
            continue

        # Filter files that support Minecraft 1.20.1 (or 1.20)
        target_mc = "1.20.1"
        filtered_files = []
        for f in all_files:
            game_versions = f.get("gameVersions", [])
            # gameVersions is a list of strings like "1.20.1"
            if any(target_mc in gv or "1.20" in gv for gv in game_versions):
                filtered_files.append(f)
        # If none support 1.20.1, use all files (but prefer those that have at least some MC version)
        if not filtered_files:
            filtered_files = all_files

        # ----- Match the file -----
        matched = None

        # 1. Exact filename (case‑insensitive)
        for f in filtered_files:
            if f.get("fileName") and f["fileName"].lower() == jar_name.lower():
                matched = f
                logger.debug(f"Exact file name match: {jar_name} -> file={f['id']}")
                break

        # 2. Extract version from filename and match
        if not matched:
            version_pattern = re.compile(r'[-_]?(\d+\.\d+(?:\.\d+)?(?:[+.-]\w+)?)[-_]')
            match = version_pattern.search(jar_name)
            extracted_version = match.group(1) if match else None
            if extracted_version:
                for f in filtered_files:
                    fname = f.get("fileName", "")
                    display = f.get("displayName", "")
                    if extracted_version in fname or extracted_version in display:
                        matched = f
                        logger.debug(f"Version match (extracted {extracted_version}): {jar_name} -> file={f['id']}")
                        break
            # If that fails, try the metadata version
            if not matched and version_from_meta:
                for f in filtered_files:
                    fname = f.get("fileName", "")
                    display = f.get("displayName", "")
                    if version_from_meta in fname or version_from_meta in display:
                        matched = f
                        logger.debug(f"Meta version match: {jar_name} -> file={f['id']}")
                        break

        # 3. Slug in filename (case‑insensitive)
        if not matched:
            for f in filtered_files:
                fname = f.get("fileName", "")
                if original_slug in fname.lower() or corrected_slug in fname.lower():
                    matched = f
                    logger.debug(f"Slug-in-filename match: {jar_name} -> file={f['id']}")
                    break

        # 4. Base name match (without .jar)
        if not matched:
            base_name = jar_name.replace(".jar", "").lower()
            for f in filtered_files:
                fname = f.get("fileName", "").lower()
                display = f.get("displayName", "").lower()
                if base_name in fname or base_name in display:
                    matched = f
                    logger.debug(f"Base-name match: {jar_name} -> file={f['id']}")
                    break

        # 5. Fallback: latest file among filtered (by fileDate)
        if not matched and filtered_files:
            sorted_files = sorted(filtered_files, key=lambda f: f.get("fileDate", ""), reverse=True)
            matched = sorted_files[0]
            logger.debug(f"Latest-file fallback (MC 1.20.1): {jar_name} -> file={matched['id']} ({matched.get('fileName', 'unknown')})")

        if matched:
            results[jar] = (mod_id, matched["id"])
        else:
            logger.warning(f"No matching file found for {jar_name} (mod {mod['name']}, slug {original_slug})")

    return results


def resolve_curse_ids(jars, logger, api_key):
    """
    Main resolver: use strict search with slug corrections.
    """
    # Test API key with a simple search
    try:
        resp = curseforge_request("/v1/mods/search", api_key, params={"gameId": 432, "searchFilter": "jei", "pageSize": 1})
        if resp.get("data"):
            first = resp["data"][0]
            logger.debug(f"API test: found mod '{first['name']}' (id={first['id']}) for 'jei'")
        else:
            logger.warning("API test: search for 'jei' returned no results – API key may be invalid.")
    except Exception as e:
        logger.error(f"API test failed: {e}")

    logger.info("Resolving via strict search with slug corrections...")
    results = resolve_by_search_direct(jars, logger, api_key)
    logger.info(f"Resolved {len(results)} mods")
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
            # Use corrected slug if available
            original_slug = meta["id"].lower().replace("_", "-")
            entry["slug"] = SLUG_CORRECTIONS.get(original_slug, original_slug)
        entries_with_jar.append((entry, jar))

    # Deduplicate: keep first occurrence by mod id
    unique = {}
    for entry, jar in entries_with_jar:
        mid = entry["id"]
        if mid not in unique:
            unique[mid] = (entry, jar)

    # If resolve, get project/file IDs
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
                mgr.file(env_file, format="env")
                loaded = True
                break
        if not loaded:
            logger.error("No .env file found. Checked: " + ", ".join(str(p) for p in env_candidates))
            sys.exit(1)

        config = mgr.load()
        api_key = config.get("CF_API_KEY") or config.get("api_key")
        if not api_key:
            logger.error("CF_API_KEY not found in .env; cannot resolve CurseForge IDs.")
            sys.exit(1)
        logger.debug(f"Loaded API key (first 4 chars): {api_key[:4]}")

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