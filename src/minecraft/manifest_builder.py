"""
manifest_builder.py - Build a manifest.yaml from .jar files or from a Prism .index folder.
Scans directories, reads mods.toml metadata, or parses Prism .pw.toml files.
"""

import argparse
import re
import sys
import tomllib
import zipfile
from pathlib import Path

import yaml
from LoggingCore import get_core, get_logger, setup_logging

from .common import config as cfg
from .common import curseforge as cf

# ----------------------------------------------------------------------
# Slug correction mapping (unchanged, used for JAR scanning)
# ----------------------------------------------------------------------
SLUG_CORRECTIONS = {
    "ftbessentials": "ftb-essentials",
    "ftblibrary": "ftb-library",
    "ftbteams": "ftb-teams-forge",
    "ftbquests": "ftb-quests-forge",
    "ftbchunks": "ftb-chunks-forge",
    "ftbfiltersystem": "ftb-filter-system",
    "ftbxmodcompat": "ftb-xmod-compat",
    "naturescompass": "natures-compass",
    "explorerscompass": "explorers-compass",
    "inventoryessentials": "inventory-essentials",
    "sophisticatedcore": "sophisticated-core",
    "sophisticatedbackpacks": "sophisticated-backpacks",
    "resourcefullib": "resourceful-lib",
    "forgeconfigscreens": "config-menus-forge",
    "puzzleslib": "puzzles-lib",
    "anvianslib": "anvians-lib",
    "createaddition": "create-addition",
    "create-new-age": "create-new-age",
    "create-hypertube": "hypertubes",
    "create-unbreakable": "create-unbreakable-tools",
    "create-decoration": "create-deco",
    "create-ultimate-factory": "create-ultimate-factory",
    "create": "create",
    "jeimultiblocks": "jei-multiblocks",
    "justenoughprofessions": "just-enough-professions-jep",
    "storagedrawers": "storage-drawers",
    "storagedrawersextra": "storage-drawers-extra",
    "underground-village": "underground-villages-stoneholm",
    "morerelics": "more-relics",
    "bits-n-bobs": "create-bits-n-bobs",
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
    "mininggadgets": "mining-gadgets",
    "inventory-sorter": "inventory-sorter",
    "enchanting-infuser": "enchanting-infuser",
    "small-ships": "small-ships",
    "architectury": "architectury-api",
    "immersive-engineering": "immersive-engineering",
}


# ----------------------------------------------------------------------
# Prism .pw.toml parser
# ----------------------------------------------------------------------


def parse_prism_toml(toml_path: Path) -> dict | None:
    """Parse a Prism .pw.toml file and return a manifest entry dict."""
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:  # noqa: BLE001
        # Logged by caller
        return None

    # Basic required fields
    filename = data.get("filename")
    name = data.get("name", "")
    side = data.get("side", "both").lower()
    if not side:
        side = "both"

    # CurseForge update info
    cf_update = data.get("update", {}).get("curseforge")
    project_id = cf_update.get("project-id") if cf_update else None
    file_id = cf_update.get("file-id") if cf_update else None

    # Modrinth update info (for future support)
    mr_update = data.get("update", {}).get("modrinth")
    if mr_update:
        # We don't support Modrinth yet; skip or set source='local'
        return None

    if not project_id or not file_id:
        # Only CurseForge supported for automatic download
        return None

    # Build entry
    entry = {
        "id": str(project_id),  # use project_id as unique ID
        "source": "curseforge",
        "file": filename,
        "side": side,
        "enabled": True,
        "project_id": project_id,
        "file_id": file_id,
        "version": "",  # we don't extract version; mod_puller uses IDs
        "display_name": name,
        "depends": [],
        "conflicts": [],
        "tags": [],
    }
    return entry


def build_manifest_from_prism(index_dir: Path, logger) -> list:
    """Read all *.pw.toml files from index_dir and build manifest entries."""
    entries = []
    for toml_path in index_dir.glob("*.pw.toml"):
        entry = parse_prism_toml(toml_path)
        if entry is None:
            logger.warning(
                f"Skipping {toml_path.name} (missing CurseForge data or unsupported)"
            )
            continue
        entries.append(entry)
        logger.debug(
            f"Added {toml_path.name} -> {entry['id']} ({entry['display_name']})"
        )

    # Deduplicate by id (project_id)
    seen = set()
    unique = []
    for entry in entries:
        if entry["id"] not in seen:
            seen.add(entry["id"])
            unique.append(entry)
        else:
            logger.warning(
                f"Duplicate mod id {entry['id']} ({entry['display_name']}) skipped"
            )

    return sorted(unique, key=lambda x: x["id"])


# ----------------------------------------------------------------------
# Legacy JAR scanning functions (unchanged)
# ----------------------------------------------------------------------


def find_jars(directories, logger):
    for d in directories:
        root = Path(d)
        if not root.is_dir():
            logger.warning(f"{root} is not a directory, skipping")
            continue
        yield from root.rglob("*.jar")


def extract_metadata(jar_path, logger):
    try:
        with zipfile.ZipFile(jar_path, "r") as zf:
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
            for dep in mod.get("dependencies", []):
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
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error reading {jar_path}: {e}")
        return None


def resolve_by_search_direct(jars, logger, api_key):
    results = {}
    for jar in jars:
        meta = extract_metadata(jar, logger)
        if not meta:
            continue

        original_slug = meta["id"].lower().replace("_", "-")
        corrected_slug = SLUG_CORRECTIONS.get(original_slug, original_slug)
        display_name = meta.get("display_name")
        version_from_meta = meta.get("version")
        jar_name = jar.name

        mod = None

        if corrected_slug != original_slug:
            mod = cf.find_mod_by_slug(corrected_slug, api_key)
            if mod:
                logger.debug(
                    f"Found mod by corrected slug '{corrected_slug}': {mod['name']} (id={mod['id']}) for {jar_name}"
                )

        if not mod:
            mod = cf.find_mod_by_slug(original_slug, api_key)
            if mod:
                logger.debug(
                    f"Found mod by original slug '{original_slug}': {mod['name']} (id={mod['id']}) for {jar_name}"
                )

        if not mod and display_name:
            try:
                resp = cf.curseforge_request(
                    "/v1/mods/search",
                    api_key,
                    params={"gameId": 432, "searchFilter": display_name, "pageSize": 5},
                )
                candidates = resp.get("data", [])
                if candidates:
                    for cand in candidates:
                        cand_slug = cand.get("slug", "")
                        if original_slug in cand_slug or corrected_slug in cand_slug:
                            mod = cand
                            break
                    if not mod:
                        mod = candidates[0]
                    logger.debug(
                        f"Found mod by display_name: {mod['name']} (id={mod['id']}) for {jar_name}"
                    )
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Display_name search failed: {e}")

        if not mod:
            logger.debug(
                f"No mod found for {jar_name} (slugs: {original_slug}/{corrected_slug})"
            )
            continue

        mod_id = mod["id"]
        all_files = cf.fetch_all_files(mod_id, api_key)
        if not all_files:
            logger.debug(f"No files found for mod {mod_id} (slug {original_slug})")
            continue

        target_mc = "1.20.1"
        filtered_files = [
            f
            for f in all_files
            if any(target_mc in gv or "1.20" in gv for gv in f.get("gameVersions", []))
        ]
        if not filtered_files:
            filtered_files = all_files

        matched = None

        for f in filtered_files:
            if f.get("fileName") and f["fileName"].lower() == jar_name.lower():
                matched = f
                break

        if not matched:
            version_pattern = re.compile(r"[-_]?(\d+\.\d+(?:\.\d+)?(?:[+.-]\w+)?)[-_]")
            match = version_pattern.search(jar_name)
            extracted_version = match.group(1) if match else None
            if extracted_version:
                for f in filtered_files:
                    fname = f.get("fileName", "")
                    display = f.get("displayName", "")
                    if extracted_version in fname or extracted_version in display:
                        matched = f
                        break
            if not matched and version_from_meta:
                for f in filtered_files:
                    fname = f.get("fileName", "")
                    display = f.get("displayName", "")
                    if version_from_meta in fname or version_from_meta in display:
                        matched = f
                        break

        if not matched:
            for f in filtered_files:
                fname = f.get("fileName", "")
                if original_slug in fname.lower() or corrected_slug in fname.lower():
                    matched = f
                    break

        if not matched:
            base_name = jar_name.replace(".jar", "").lower()
            for f in filtered_files:
                fname = f.get("fileName", "").lower()
                display = f.get("displayName", "").lower()
                if base_name in fname or base_name in display:
                    matched = f
                    break

        if not matched and filtered_files:
            matched = max(filtered_files, key=lambda f: f.get("fileDate", ""))

        if matched:
            results[jar] = (mod_id, matched["id"])
        else:
            logger.warning(
                f"No matching file found for {jar_name} (mod {mod['name']}, slug {original_slug})"
            )

    return results


def build_manifest_from_jars(
    jars, default_side, logger, curse=False, resolve=False, api_key=None
):
    entries_with_jar = []
    for jar in jars:
        meta = extract_metadata(jar, logger)
        if meta is None:
            continue

        side = (
            meta["side"]
            if meta["side"] in ["client", "server", "both"]
            else default_side
        )
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
            original_slug = meta["id"].lower().replace("_", "-")
            entry["slug"] = SLUG_CORRECTIONS.get(original_slug, original_slug)
        entries_with_jar.append((entry, jar))

    unique = {}
    for entry, jar in entries_with_jar:
        if entry["id"] not in unique:
            unique[entry["id"]] = (entry, jar)

    if resolve and api_key:
        kept_jars = [jar for _, jar in unique.values()]
        logger.info(f"Resolving CurseForge IDs for {len(kept_jars)} unique mods...")
        resolved = resolve_by_search_direct(kept_jars, logger, api_key)
        resolved_count = 0
        for entry, jar in unique.values():
            if jar in resolved:
                project_id, file_id = resolved[jar]
                entry["project_id"] = project_id
                entry["file_id"] = file_id
                resolved_count += 1
        logger.info(f"Resolved {resolved_count}/{len(unique)} mods")

    return sorted(
        [entry for entry, _ in unique.values()], key=lambda x: x["id"].lower()
    )


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Build a manifest.yaml from .jar files or Prism .index."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--mods",
        action="append",
        help="Directories to scan for .jar files (can be used multiple times)",
    )
    group.add_argument(
        "--prism-index",
        type=Path,
        help="Path to Prism launcher .index folder (contains *.pw.toml files)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("config.d/manifest.yaml"),
        help="Output manifest file (default: config.d/manifest.yaml)",
    )
    parser.add_argument(
        "--side",
        default="both",
        choices=["client", "server", "both"],
        help="Default side for mods that don't specify one (default: both)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--no-deduplicate",
        action="store_true",
        help="Disable deduplication (keep duplicate entries) - only for JAR scanning",
    )
    parser.add_argument(
        "--curse",
        action="store_true",
        help="Configure entries for CurseForge: add slug and source: curseforge (JAR mode only)",
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="Query CurseForge API to fill project_id and file_id (implies --curse, JAR mode only)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to .env file (default: checks ./.env and config.d/.env)",
    )

    args = parser.parse_args()

    if args.resolve and not args.curse:
        args.curse = True

    if args.prism_index and (args.curse or args.resolve):
        print(
            "Warning: --curse and --resolve are ignored when using --prism-index.",
            file=sys.stderr,
        )

    # Setup logging
    log_config = {
        "color": True,
        "handlers": [
            {"type": "console", "color": True},
            {
                "type": "file",
                "path": "logs/manifest_builder.log",
                "max_bytes": 10_485_760,
                "backup_count": 5,
            },
        ],
    }
    setup_logging(log_config)
    logger = get_logger(__name__)

    core = get_core()
    if core is not None:
        core.set_level(10 if args.debug else 20)

    # Load API key if resolving (JAR mode)
    api_key = None
    if args.resolve:
        env_candidates = [args.env_file] if args.env_file else []
        env_candidates += [Path(".env"), Path("config.d/.env")]
        config = cfg.load_combined_config(
            config_dir=Path("config.d"),
            base_name="manifest_builder",
            env_prefix="",
            env_file_candidates=env_candidates,
        )
        api_key = config.get("CF_API_KEY") or config.get("api_key")
        if not api_key:
            logger.error("CF_API_KEY not found; cannot resolve CurseForge IDs.")
            sys.exit(1)
        logger.debug(f"Loaded API key (first 4 chars): {api_key[:4]}")

    logger.info("Starting manifest builder")
    logger.info(f"  Output manifest: {args.manifest}")
    logger.info(f"  Default side: {args.side}")

    if args.prism_index:
        logger.info(f"  Mode: Prism index from {args.prism_index}")
        if not args.prism_index.is_dir():
            logger.error(f"Prism index directory not found: {args.prism_index}")
            sys.exit(1)
        manifest_entries = build_manifest_from_prism(args.prism_index, logger)
        if not manifest_entries:
            logger.error("No valid entries found in Prism index.")
            sys.exit(1)
    else:
        logger.info(f"  Mode: JAR scanning from {args.mods}")
        jars = list(find_jars(args.mods, logger))
        if not jars:
            logger.error("No .jar files found.")
            sys.exit(1)
        logger.info(f"Found {len(jars)} jar files.")
        manifest_entries = build_manifest_from_jars(
            jars,
            args.side,
            logger,
            curse=args.curse,
            resolve=args.resolve,
            api_key=api_key,
        )
        if not manifest_entries:
            logger.error("No valid mod metadata found.")
            sys.exit(1)

    # Write manifest
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.open("w") as f:
        yaml.dump(
            {"mods": manifest_entries}, f, default_flow_style=False, sort_keys=False
        )

    logger.info(f"Manifest written to {args.manifest}")
    logger.info(f"Total mods: {len(manifest_entries)}")


if __name__ == "__main__":
    main()
