# src/minecraft/deploy_pack.py

"""deploy_pack.py - Generate server ZIP and update live server.

Uses Prism .index for mod metadata and ConfigCore for configuration.

Multi-instance model:
    sync_root  ──► each instance  (config, kubejs, ...)
               ──► shared mods    (from Prism index, deployed once)
               ──► shared www     (@www/... destinations)
               ──► client ZIP     (www_dir/minecraft_client_<date>.zip)

After the client ZIP is written, the deploy publishes an HTML changelog
page to www_dir and posts a short announcement to Discord pointing at
that page. The page carries the download link, the SHA-256, and the
source-vs-target diff. Discord carries only a short pointer plus the
SHA-256 so the announcement itself is verifiable at a glance.

No network calls are made to any model service. The changelog is
computed locally by diffing source and target directories.

Test modes:

    --dry-run            Build ZIP and changelog into <www_dir>/dry_run/,
                         skip every live-server write, and print the
                         Discord payload to the console instead of
                         posting it. Implies --no-deploy.

    --dry-run-notify     With --dry-run, actually post the webhook
                         message. For testing Discord format without
                         touching the live servers.

    --no-notify          Build the ZIP and changelog HTML but do not
                         post the Discord webhook. Combine with
                         --no-deploy to produce both www artifacts
                         without touching live servers and without
                         sending anything to Discord.

    --debug-deps         Print the dependency closure for both sides
                         and exit. Read-only against the index and
                         the jars; touches nothing else.
"""

import argparse
import datetime
import fnmatch
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from LoggingCore import get_logger, setup_logging

from .common import changelog, deps, file_utils, notify, overrides, prism
from .common import config as cfg

# ---------------------------------------------------------------------------
# Mod loading
# ---------------------------------------------------------------------------


def load_mod_list(prism_index: Path, config_dir: Path, target_side: str, logger) -> list:
    """Load mod entries from a Prism index directory, filtered by target side.

    After the side filter runs, a dependency closure pulls in any
    transitively-required entry that the filter would have excluded.
    This prevents shipping a pack where Forge's load-time dependency
    check fails with "mod X requires Y, Y is not installed".

    The closure reads both the index's own dependency metadata
    ([[x-prismlauncher-dependencies]] blocks, project-id namespace)
    and each jar's META-INF/mods.toml (modId namespace). See
    common/deps.py for details on how the two namespaces are bridged.

    Nothing is written back to the index. When upstream metadata
    improves, the closure simply gets smaller on the next build.
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

    result = deps.expand_with_required(
        all_entries=all_mods,
        seed_entries=side_mods,
        target_side=target_side,
        modpack_dir=prism_index.parent,
        logger=logger,
    )
    if result.forced_count:
        logger.info(f"Dependency closure for side '{target_side}': force-included {result.forced_count} mod(s) not in the side filter")
        for dependent, dependency, reason in result.forced:
            logger.info(f"  {dependent.get('file')} -> {dependency.get('file')} ({reason})")

    return result.entries


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def resolve_mapping_for_side(mapping_value, side: str) -> str | None:
    """Return the destination string for a given side, or None if excluded.

    mapping_value can be:
      - str: used for both sides
      - dict with 'server'/'client' keys: side-specific
      - -1 or missing side key: excluded for that side
    """
    if isinstance(mapping_value, str):
        return mapping_value
    if isinstance(mapping_value, dict):
        v = mapping_value.get(side)
        if v is None or v == -1:
            return None
        if not isinstance(v, str):
            return None
        return v
    return None


def is_shared_dest(dest: str) -> bool:
    """Return True if the destination refers to a shared (non-instance) location."""
    return dest.startswith("@")


def resolve_shared_dest(dest: str, www_dir: Path, mods_dir: Path) -> Path:
    """Resolve '@www/foo' to www_dir/foo, '@mods/foo' to mods_dir/foo."""
    if dest == "@www":
        return www_dir
    if dest.startswith("@www/"):
        return www_dir / dest[5:]
    if dest == "@mods":
        return mods_dir
    if dest.startswith("@mods/"):
        return mods_dir / dest[6:]
    raise ValueError(f"Unknown shared destination prefix: {dest}")


def iter_sync_items(sync_root: Path, exclude_patterns: list, logger):
    """Yield (rel, item) for each top-level sync item, skipping downloads and excluded."""
    for item in sync_root.iterdir():
        rel = Path(item.name)
        if rel.parts[0] == "downloads":
            continue
        if any(fnmatch.fnmatch(str(rel), pat) for pat in exclude_patterns):
            logger.debug(f"Skipping excluded top-level item: {rel}")
            continue
        yield rel, item


def _copy_item_to(item: Path, dest_path: Path, exclude_patterns: list, logger):
    """Copy a sync item (file or dir) to dest_path, applying exclusions recursively."""
    if item.is_dir():
        file_utils.copy_with_exclusions(item, dest_path, exclude_patterns, logger, clean=False)
    else:
        if any(fnmatch.fnmatch(item.name, pat) for pat in exclude_patterns):
            logger.debug(f"Skipping excluded file: {item.name}")
            return
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dest_path)


# ---------------------------------------------------------------------------
# Staging builders
# ---------------------------------------------------------------------------


def prepare_mods_staging(side_mods: list, modpack_dir: Path, logger) -> Path:
    """Create a staging dir containing only the mods for the given side."""
    staging = Path(tempfile.mkdtemp(prefix="deploy_mods_"))
    mods_subdir = staging / "mods"
    mods_subdir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for entry in side_mods:
        file_rel = entry.get("file")
        if not file_rel:
            logger.warning("Mod entry missing 'file' field, skipping")
            continue
        src = modpack_dir / file_rel
        dst = mods_subdir / file_rel
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
    logger.info(f"Copied {copied}/{len(side_mods)} mod files to mods staging")
    return staging


def prepare_instance_staging(
    sync_root: Path,
    side: str,
    exclude_patterns: list,
    sync_mapping: dict,
    logger,
) -> Path:
    """Create a staging dir with all NON-shared sync items for one instance."""
    staging = Path(tempfile.mkdtemp(prefix="deploy_instance_"))
    logger.debug(f"Instance staging directory: {staging}")

    for rel, item in iter_sync_items(sync_root, exclude_patterns, logger):
        key = str(rel)
        mapping_value = sync_mapping.get(key)
        if mapping_value is None:
            logger.debug(f"Item '{key}' not in sync_mapping, skipped.")
            continue

        dest_rel = resolve_mapping_for_side(mapping_value, side)
        if dest_rel is None:
            logger.debug(f"Item '{key}' ignored for side '{side}'")
            continue

        if is_shared_dest(dest_rel):
            logger.debug(f"Item '{key}' -> '{dest_rel}' is shared, skipping in instance staging")
            continue

        dest_path = staging / dest_rel
        _copy_item_to(item, dest_path, exclude_patterns, logger)

    return staging


def prepare_client_staging(
    client_mods: list,
    modpack_dir: Path,
    sync_root: Path,
    exclude_patterns: list,
    sync_mapping: dict,
    logger,
) -> Path:
    """Build a client staging dir (client mods + client-mapped sync items)."""
    staging = Path(tempfile.mkdtemp(prefix="deploy_client_"))
    logger.info(f"Client staging directory: {staging}")

    mods_dir = staging / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for entry in client_mods:
        file_rel = entry.get("file")
        if not file_rel:
            continue
        src = modpack_dir / file_rel
        dst = mods_dir / file_rel
        if not file_utils.ensure_mod_file(
            src,
            entry.get("download_url"),
            entry.get("hash_value"),
            entry.get("hash_format", "sha512"),
            logger,
        ):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    logger.info(f"Copied {copied}/{len(client_mods)} mod files to client staging")

    for rel, item in iter_sync_items(sync_root, exclude_patterns, logger):
        key = str(rel)
        mapping_value = sync_mapping.get(key)
        if mapping_value is None:
            continue
        dest_rel = resolve_mapping_for_side(mapping_value, "client")
        if dest_rel is None:
            continue
        if is_shared_dest(dest_rel):
            logger.debug(f"Skipping shared item for client ZIP: {key} -> {dest_rel}")
            continue
        dest_path = staging / dest_rel
        _copy_item_to(item, dest_path, exclude_patterns, logger)

    return staging


# ---------------------------------------------------------------------------
# Deployment actions
# ---------------------------------------------------------------------------


def deploy_shared_items(
    sync_root: Path,
    side: str,
    exclude_patterns: list,
    sync_mapping: dict,
    www_dir: Path,
    mods_dir: Path,
    logger,
):
    """Copy all @-prefixed sync items to their shared destinations."""
    for rel, item in iter_sync_items(sync_root, exclude_patterns, logger):
        key = str(rel)
        mapping_value = sync_mapping.get(key)
        if mapping_value is None:
            continue

        dest_rel = resolve_mapping_for_side(mapping_value, side)
        if dest_rel is None or not is_shared_dest(dest_rel):
            continue

        dest_path = resolve_shared_dest(dest_rel, www_dir, mods_dir)
        _copy_item_to(item, dest_path, exclude_patterns, logger)
        logger.info(f"Copied shared item {rel} -> {dest_path}")


def create_client_zip(
    staging_dir: Path,
    output_dir: Path,
    filename_template: str,
    exclude_patterns: list,
    logger,
) -> Path:
    """Create a ZIP archive from the staging directory and return its path.

    ``output_dir`` is the directory the ZIP lands in. In a normal deploy
    that is ``www_dir``. In a dry-run it is ``<www_dir>/dry_run``.
    """
    date_str = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")
    zip_name = filename_template.format(date=date_str)
    output_zip = output_dir / zip_name
    logger.info(f"Creating zip: {output_zip}")
    file_utils.create_zip_from_staging(staging_dir, output_zip, exclude_patterns, logger)
    logger.info(f"Client pack created successfully at {output_zip}")
    return output_zip


def publish_release(
    client_zip: Path,
    report: "changelog.DiffReport",
    config,
    output_dir: Path,
    logger,
    dry_run: bool = False,
    dry_run_notify: bool = False,
    no_notify: bool = False,
) -> None:
    """Write the changelog HTML page and post the announcement to Discord.

    ``output_dir`` is where the changelog HTML lands. In a normal deploy
    that is ``www_dir``. In a dry-run it is ``<www_dir>/dry_run``.

    Notification modes (mutually exclusive behavior, checked in order):

      - no_notify:           skip Discord entirely. HTML still written.
      - dry_run + !notify:   print the payload, do not post.
      - dry_run + notify:    post the payload (format test).
      - neither flag:        post the payload (normal deploy).

    A failed post is logged but never raised - a notification problem
    must not fail a deploy.
    """
    artifact_name = client_zip.name
    download_base = config.get("download_base_url", "") or ""

    if download_base:
        artifact_url = download_base.rstrip("/") + "/" + artifact_name
    else:
        artifact_url = artifact_name

    try:
        sha256sum = file_utils.compute_file_hash(client_zip, "sha256")
    except OSError as exc:
        logger.warning(f"Could not compute SHA-256 for {client_zip}: {exc}")
        sha256sum = "unavailable"

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
    date_str = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")

    # --- Write the changelog page ------------------------------------
    changelog_name = f"changelog_{date_str}.html"
    changelog_path = output_dir / changelog_name
    changelog.write_changelog(
        report=report,
        artifact_name=artifact_name,
        artifact_url=artifact_url,
        sha256sum=sha256sum,
        timestamp=timestamp,
        output_path=changelog_path,
    )
    logger.info(f"Wrote changelog page: {changelog_path}")

    if download_base:
        changelog_url = download_base.rstrip("/") + "/" + changelog_name
    else:
        changelog_url = changelog_name

    # --- Skip Discord entirely if asked ------------------------------
    if no_notify:
        logger.info("Skipping Discord notification (--no-notify).")
        return

    # --- Build the webhook message -----------------------------------
    template = config.get(
        "webhook_message_template",
        "**Minecraft Client Pack Published**\n\n"
        "**Update notes:** {changelog_url}\n"
        "**Download:** {url}\n\n"
        "**SHA-256**\n`{sha256sum}`\n\n"
        "**Summary**\n{summary}\n\n"
        "Built {timestamp} UTC",
    )
    context = {
        "artifact": artifact_name,
        "url": artifact_url,
        "changelog_url": changelog_url,
        "sha256sum": sha256sum,
        "timestamp": timestamp,
        "summary": report.summary_line(),
    }
    try:
        content = template.format(**context)
    except KeyError as exc:
        logger.error(f"Unknown placeholder in webhook_message_template: {exc}")
        content = f"New client pack: {changelog_url}"

    # --- Dry-run: print instead of posting ---------------------------
    if dry_run and not dry_run_notify:
        logger.info("=" * 72)
        logger.info("DRY-RUN: Discord payload that would be posted")
        logger.info("-" * 72)
        for line in content.splitlines():
            logger.info(f"  {line}")
        logger.info("-" * 72)
        logger.info(f"  payload length: {len(content)} chars (limit 2000)")
        logger.info("=" * 72)
        return

    # --- Real send ---------------------------------------------------
    webhook_url = config.get("webhook_url")
    if not webhook_url:
        logger.debug("No webhook_url configured, skipping notification")
        return

    if notify.post_discord_webhook(webhook_url, content):
        logger.info("Posted client pack announcement")
    else:
        logger.warning("Client pack announcement failed (deploy succeeded)")


def deploy_to_server(
    staging_dir: Path,
    live_server: Path,
    exclude_patterns: list,
    protect_patterns: list,
    logger,
):
    """Copy staging contents to a live server instance (with cleanup)."""
    logger.info(f"Deploying to live_server: {live_server}")
    file_utils.copy_with_exclusions(
        staging_dir,
        live_server,
        exclude_patterns,
        logger,
        clean=True,
        protect_patterns=protect_patterns,
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


def load_instances(config, logger) -> list[tuple[str, Path]]:
    """Load instance name -> path pairs from config.

    Supports both shapes:
        [instances.survival]
        path = "./survival"

        instances = { survival = "./survival", creative = "./creative" }
    """
    instances = config.get("instances")
    if not instances:
        return []
    result: list[tuple[str, Path]] = []
    for name, value in instances.items():
        if isinstance(value, dict):
            path_str = value.get("path")
        else:
            path_str = value
        if not path_str:
            logger.warning(f"Instance '{name}' has no path, skipping")
            continue
        result.append((name, Path(path_str)))
    return result


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    """Main entrypoint for deploy_pack.py."""
    parser = argparse.ArgumentParser(description="Deploy modpack")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--server", action="store_true", help="Server mode (default)")
    group.add_argument("--client", action="store_true", help="Client mode: deploy to MultiMC instance")
    parser.add_argument(
        "--config-dir",
        type=str,
        default=None,
        help="Path to config directory (default: config.d). If a file is given, its parent is used.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging and full traceback")
    parser.add_argument("--no-deploy", action="store_true", help="With --server: skip writing to instances/mods/www (only create ZIP).")
    parser.add_argument("--no-zip", action="store_true", help="With --server: skip creating the client ZIP (only deploy server side).")
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help=(
            "With --server: build the client ZIP and changelog HTML but do "
            "not post the Discord webhook. Combine with --no-deploy to "
            "produce both www artifacts without touching live servers."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "With --server: skip every live-server write. Build the client "
            "ZIP and changelog HTML into <www_dir>/dry_run/. Print the "
            "Discord payload to the console instead of posting it. Implies "
            "--no-deploy."
        ),
    )
    parser.add_argument(
        "--dry-run-notify",
        action="store_true",
        help=("With --dry-run: actually post the webhook message instead of only printing it. For testing the Discord format without touching live servers."),
    )
    parser.add_argument(
        "--debug-deps",
        action="store_true",
        help=(
            "Print the dependency closure for both sides and exit. "
            "Shows which entries the side filter excludes but the closure "
            "pulls back in, and why. Reads the index and every jar's "
            "manifest; touches nothing else. Safe to run on a live tree."
        ),
    )
    args, remaining = parser.parse_known_args()

    mode = "client" if args.client else "server"

    # --dry-run implies --no-deploy.
    if args.dry_run and not args.no_deploy:
        args.no_deploy = True

    if args.dry_run_notify and not args.dry_run:
        parser.error("--dry-run-notify requires --dry-run")

    if args.config_dir:
        config_dir = Path(args.config_dir)
        if config_dir.is_file():
            config_dir = config_dir.parent
    else:
        config_dir = Path(os.environ.get("DEPLOYPACK_CONFIG_DIR", "config.d"))

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
            if any(token in key.lower() for token in ("webhook", "secret", "token", "password")):
                value = "<redacted>"
            print(f"{key} = {value}")
        print("============================")

    def get_path(key: str, default: str) -> Path:
        val = config.get(key)
        return Path(val) if val is not None else Path(default)

    sync_root = get_path("sync_root", "./sync")
    mods_dir = get_path("mods_dir", "./mods")
    www_dir = get_path("www_dir", "./www")
    exclude_file = get_path("exclude_file", "./.rsync_exclude")
    protect_file = get_path("protect_file", "./.deploy_protect")
    output_filename = config.get("output_filename", "minecraft_client_{date}.zip")
    modpack_dir = get_path("modpack_dir", "./sync/downloads")
    sync_mapping = config.get("sync_mapping", {})
    multimc_base = config.get("multimc_base", str(Path.home() / ".local/share/multimc/instances"))
    instance_name = config.get("instance_name")

    prism_index_dir = modpack_dir / ".index"

    # Output directory for ZIP + changelog HTML. In a dry-run it is a
    # subdirectory of www_dir so nothing overwrites the live artifacts.
    if args.dry_run:
        output_dir = www_dir / "dry_run"
    else:
        output_dir = www_dir

    log_config = config.get("logging")
    if not log_config:
        log_config = {
            "color": True,
            "handlers": [
                {"type": "console", "color": True},
                {"type": "file", "path": "logs/deploy_pack.log", "max_bytes": 10_485_760, "backup_count": 5},
            ],
        }
    log_config["level"] = "DEBUG" if args.debug else "INFO"
    for h in log_config.get("handlers", []):
        h["level"] = log_config["level"]

    setup_logging(log_config)
    logger = get_logger(__name__)

    # ------------------------------------------------------------------
    # --debug-deps: read-only diagnostic. Exits before any deploy logic
    # runs, so it is safe to invoke against a live tree.
    # ------------------------------------------------------------------
    if args.debug_deps:
        if not prism_index_dir.is_dir():
            logger.error(f"Prism index directory not found: {prism_index_dir}")
            sys.exit(1)
        all_mods = prism.load_prism_index(prism_index_dir)
        if not all_mods:
            logger.error(f"No mod entries found in {prism_index_dir}")
            sys.exit(1)
        override_path = config_dir / "side_overrides.toml"
        overrides_data = overrides.load_side_overrides(override_path)
        if overrides_data:
            all_mods = overrides.apply_side_overrides(all_mods, overrides_data)
        seeds = {side: prism.filter_prism_entries_by_side(all_mods, side) for side in ("client", "server")}
        print(
            deps.format_diagnostic(
                all_entries=all_mods,
                seeds=seeds,
                modpack_dir=prism_index_dir.parent,
                logger=logger,
            )
        )
        return

    instances = load_instances(config, logger)

    if args.dry_run:
        logger.info("=" * 72)
        logger.info("DRY-RUN MODE")
        logger.info("  Live servers, shared mods, and shared www items will")
        logger.info("  NOT be touched. Output goes to:")
        logger.info(f"    {output_dir}")
        if args.dry_run_notify:
            logger.info("  Webhook WILL be posted (--dry-run-notify).")
        else:
            logger.info("  Webhook payload will be printed, not posted.")
        logger.info("=" * 72)

    if args.no_notify and not args.dry_run:
        logger.info("--no-notify: Discord webhook will be skipped.")

    logger.info(f"Mode: {mode}")
    logger.info(f"  config_dir    = {config_dir}")
    logger.info(f"  sync_root     = {sync_root}")
    logger.info(f"  mods_dir      = {mods_dir}")
    logger.info(f"  www_dir       = {www_dir}")
    logger.info(f"  output_dir    = {output_dir}")
    logger.info(f"  modpack_dir   = {modpack_dir}")
    logger.info(f"  prism_index   = {prism_index_dir}")
    logger.info(f"  exclude_file  = {exclude_file}")
    logger.info(f"  protect_file  = {protect_file}")
    logger.info(f"  instances     = {[(n, str(p)) for n, p in instances]}")
    if mode == "client":
        logger.info(f"  multimc_base  = {multimc_base}")
        logger.info(f"  instance_name = {instance_name}")

    if mode == "client" and not instance_name:
        logger.error("instance_name must be set for client mode")
        sys.exit(1)

    if mode == "server" and not instances and not args.no_deploy:
        logger.error("No instances defined under [instances.*] - nothing to deploy to.")
        sys.exit(1)

    if mode == "server" and args.no_zip and args.no_deploy:
        logger.warning("Both --no-zip and --no-deploy specified - nothing will be done.")

    exclude_patterns = file_utils.get_exclude_patterns(exclude_file, logger)
    protect_patterns = file_utils.get_protect_patterns(protect_file, logger)

    # ------------------------------------------------------------------
    # Pre-deploy snapshot: what will change when this deploy runs.
    #
    # Computed BEFORE we touch anything, because after the deploy the
    # target will match the source and the diff would always be empty.
    # Errors here are non-fatal.
    # ------------------------------------------------------------------
    pre_deploy_report: changelog.DiffReport = changelog.DiffReport()
    if mode == "server" and not args.no_zip:
        try:
            server_mods_for_diff = load_mod_list(prism_index_dir, config_dir, "server", logger)
            wanted_mod_files = [e["file"] for e in server_mods_for_diff if e.get("file")]
            first_instance_kubejs = (instances[0][1] / "kubejs") if instances else Path()
            pre_deploy_report = changelog.build_diff_report(
                wanted_mod_files=wanted_mod_files,
                target_mods_dir=mods_dir,
                source_kubejs=sync_root / "kubejs",
                target_kubejs=first_instance_kubejs,
            )
            logger.info(f"Pre-deploy changelog: {pre_deploy_report.summary_line()}")
        except (ValueError, FileNotFoundError) as exc:
            logger.warning(f"Could not build pre-deploy diff: {exc}")

    try:
        # Ensure the output directory exists. Only do this when we are
        # actually going to write to it, and only ever a directory that
        # is either the real www_dir or its dry_run/ subdirectory.
        #
        # This runs inside the try block so that a permission error on
        # the target filesystem is logged with the same traceback
        # handling as every other failure in the deploy pipeline.
        if not args.no_zip:
            output_dir.mkdir(parents=True, exist_ok=True)

        if mode == "server":
            # ---------- Server deployment ----------
            if not args.no_deploy:
                # 1. Deploy shared server mods once into mods_dir
                server_mods = load_mod_list(prism_index_dir, config_dir, "server", logger)
                staging_mods = prepare_mods_staging(server_mods, modpack_dir, logger)
                try:
                    mods_src = staging_mods / "mods"
                    if mods_src.is_dir():
                        file_utils.copy_with_exclusions(
                            mods_src,
                            mods_dir,
                            exclude_patterns,
                            logger,
                            clean=True,
                            protect_patterns=protect_patterns,
                        )
                finally:
                    shutil.rmtree(staging_mods, ignore_errors=True)

                # 2. Deploy instance-mapped items (config, kubejs, ...) to EVERY instance
                for inst_name, inst_path in instances:
                    logger.info(f"--- Deploying to instance '{inst_name}' ({inst_path}) ---")
                    staging = prepare_instance_staging(sync_root, "server", exclude_patterns, sync_mapping, logger)
                    try:
                        deploy_to_server(
                            staging,
                            inst_path,
                            exclude_patterns,
                            protect_patterns,
                            logger,
                        )
                    finally:
                        shutil.rmtree(staging, ignore_errors=True)

                # 3. Deploy @-prefixed shared items (resourcepacks -> www, ...)
                deploy_shared_items(
                    sync_root,
                    "server",
                    exclude_patterns,
                    sync_mapping,
                    www_dir,
                    mods_dir,
                    logger,
                )
            else:
                if args.dry_run:
                    logger.info("Skipping deployment (dry-run).")
                else:
                    logger.info("Skipping deployment (--no-deploy).")

            # ---------- Client ZIP + changelog + announcement ----------
            if not args.no_zip:
                client_mods = load_mod_list(prism_index_dir, config_dir, "client", logger)
                staging_client = prepare_client_staging(
                    client_mods,
                    modpack_dir,
                    sync_root,
                    exclude_patterns,
                    sync_mapping,
                    logger,
                )
                try:
                    client_zip = create_client_zip(
                        staging_client,
                        output_dir,
                        output_filename,
                        exclude_patterns,
                        logger,
                    )
                    publish_release(
                        client_zip=client_zip,
                        report=pre_deploy_report,
                        config=config,
                        output_dir=output_dir,
                        logger=logger,
                        dry_run=args.dry_run,
                        dry_run_notify=args.dry_run_notify,
                        no_notify=args.no_notify,
                    )
                finally:
                    shutil.rmtree(staging_client, ignore_errors=True)
            else:
                logger.info("Skipping client ZIP creation (--no-zip).")

        else:
            # ---------- Client mode ----------
            client_mods = load_mod_list(prism_index_dir, config_dir, "client", logger)
            staging = prepare_client_staging(
                client_mods,
                modpack_dir,
                sync_root,
                exclude_patterns,
                sync_mapping,
                logger,
            )
            try:
                deploy_to_client(
                    staging,
                    Path(multimc_base),
                    instance_name,
                    exclude_patterns,
                    logger,
                )
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    except Exception:
        if args.debug:
            traceback.print_exc()
        logger.exception("Deployment failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
