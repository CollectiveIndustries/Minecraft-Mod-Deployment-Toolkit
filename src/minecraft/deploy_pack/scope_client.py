# src/minecraft/deploy_pack/scope_client.py

"""Client scope: ZIP, changelog HTML, and @www shared-item publication (Project_Specs.md §1.1, §2.1, §4.2, §7.1, §7.5, §7.6, §9.2).

Responsibilities:
  * assemble the client staging tree (§7.1 ZIP contents)
  * find the changelog baseline (§7.1)
  * build the diff report via common/changelog.py (§1.1)
  * create the client ZIP atomically (§4.10)
  * publish the changelog HTML atomically (§4.10)
  * publish @www/* shared items from sync_mapping (§2.1)

Non-responsibilities:
  * Resource-pack ZIP publication and server.properties updates.
    scope_resource_pack.
  * Discord notification. notifications.py.
  * Preflight checks (source existence, filename validity). preflight.
  * Unmarked-mod handling (§6.3). The audit tool (prompt_ui.py) or the
    caller decides; this scope filters unmarked entries defensively.

Staging interpretation (§7.1)
-----------------------------

§7.1 says "no staging" but §1.1 freezes common/changelog.py, whose
public API takes a staging_dir. The reconciliation: server-scope writes
go directly to instance dirs (§7.2 semantics, no swap). The client ZIP
is a single artifact; assembling its contents in a temp dir and
publishing it atomically via files.create_zip satisfies §4.10 without
violating §7.1's intent. The temp dir is not a deployment staging tree;
it is never swapped into place.

Logging
-------

The scope logs at INFO on entry, on baseline resolution, when the ZIP
and changelog land, and per shared item published. Per-key staging
decisions and count summaries log at DEBUG. Warnings the operator
needs to act on (missing RP source, unhashable ZIP, bad @www
destination) log at WARN. Failures log at ERROR with the failing stage
name. The module logger is ``minecraft.deploy_pack.scope_client``;
callers may inject an override via ``logger=`` for a single call.
"""

from __future__ import annotations

import datetime
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minecraft.common import changelog as changelog_mod

from . import deps
from .config_model import DeploymentConfig
from .files import (
    CopyResult,
    atomic_write,
    compute_sha256,
    copy_tree,
    create_zip,
    is_shared_dest,
    resolve_mapping_for_side,
    resolve_shared_dest,
)
from .logging_setup import get_logger
from .overrides import apply_side_overrides, load_side_overrides

_log = get_logger(__name__)

__all__ = [
    "ClientScopeResult",
    "deploy_client_scope",
]


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class ClientScopeResult:
    """Outcome of the client scope write phase."""

    success: bool = True
    failure_message: str | None = None

    resolved_output_filename: str | None = None
    zip_path: Path | None = None
    zip_sha256: str | None = None
    zip_url: str | None = None

    changelog_path: Path | None = None
    changelog_url: str | None = None
    report: changelog_mod.DiffReport | None = None
    initial_build: bool = False

    baseline_zip: Path | None = None

    published_shared: list[Path] = field(default_factory=list)
    shared_results: dict[str, CopyResult] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Output filename resolution and baseline (§7.1)
# ---------------------------------------------------------------------------


_DATE_TOKEN = "{date}"  # nosec


def _resolve_output_name(template: str, logger: Any = None) -> tuple[str, str]:
    """Return ``(resolved_filename, date_str)``.

    ``{date}`` is replaced with today's UTC date in YYYYMMDD form. A
    template without the token is returned unchanged (per §7.1's
    "same-date re-run when output_filename does not contain {date}"
    clause).
    """
    if logger is None:
        logger = _log
    date_str = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")
    resolved = template.replace(_DATE_TOKEN, date_str)
    logger.debug(f"_resolve_output_name: template={template!r} -> {resolved!r} (date={date_str})")
    return (resolved, date_str)


def _changelog_name(resolved_zip_name: str, logger: Any = None) -> str:
    """§7.1: replace the trailing ``.zip`` with ``.html``."""
    if logger is None:
        logger = _log
    if resolved_zip_name.endswith(".zip"):
        name = resolved_zip_name[: -len(".zip")] + ".html"
    else:
        name = resolved_zip_name + ".html"
    logger.debug(f"_changelog_name: {resolved_zip_name!r} -> {name!r}")
    return name


def _find_baseline(output_dir: Path, template: str, date_str: str, logger: Any = None) -> Path | None:
    """Find the changelog baseline ZIP per §7.1.

    Cases:

      1. Template has no ``{date}``: baseline = existing file at the
         resolved name (or None).
      2. Template has ``{date}``: look for ZIPs matching
         ``<prefix>*<suffix>``; prefer the same-date candidate if
         present; else the lexicographic maximum of the date substring.
    """
    if logger is None:
        logger = _log

    if _DATE_TOKEN not in template:
        candidate = output_dir / template
        if candidate.is_file():
            logger.debug(f"_find_baseline: no-date template; using existing {candidate}")
            return candidate
        logger.debug(f"_find_baseline: no-date template; {candidate} does not exist")
        return None

    prefix, _, suffix = template.partition(_DATE_TOKEN)
    if not prefix and not suffix:
        logger.debug("_find_baseline: template is only {date}; cannot resolve")
        return None

    pattern = f"{prefix}*{suffix}"
    candidates: list[tuple[str, Path]] = []
    if output_dir.is_dir():
        for p in output_dir.glob(pattern):
            if not p.is_file():
                continue
            name = p.name
            if not (name.startswith(prefix) and name.endswith(suffix)):
                continue
            middle = name[len(prefix) : len(name) - len(suffix)]
            if not middle:
                continue
            candidates.append((middle, p))

    if not candidates:
        logger.debug(f"_find_baseline: no candidates matching {pattern!r} in {output_dir}")
        return None

    for middle, p in candidates:
        if middle == date_str:
            logger.debug(f"_find_baseline: same-date baseline {p}")
            return p

    candidates.sort(key=lambda x: x[0], reverse=True)
    chosen = candidates[0][1]
    logger.debug(f"_find_baseline: most-recent prior baseline {chosen}")
    return chosen


# ---------------------------------------------------------------------------
# Mod source resolution
# ---------------------------------------------------------------------------


def _is_unmarked(entry: dict) -> bool:
    """§6.3: an entry whose declared ``side`` is outside the valid set.

    ``side_raw is None`` (no ``side`` key in the .pw.toml) is treated as
    marked, matching the parser's default of ``"both"``.
    """
    raw = entry.get("side_raw")
    if raw is None:
        return False
    return raw not in ("client", "server", "both")


def _resolve_client_mod_sources(config: DeploymentConfig, logger: Any) -> dict[str, Path]:
    """Return ``{filename: source_path}`` for the client-side mod set.

    Applies overrides, drops unmarked entries (unless overridden), runs
    the client side filter, and expands the dependency closure. Files
    missing from disk are skipped with a warning.
    """
    index_dir = config.modpack_dir / ".index"
    if not index_dir.is_dir():
        logger.debug(f"_resolve_client_mod_sources: index dir missing: {index_dir}")
        return {}
    entries = deps.load_prism_index(index_dir)
    if not entries:
        logger.debug(f"_resolve_client_mod_sources: no Prism entries under {index_dir}")
        return {}

    overrides_path = config.config_dir / "side_overrides.toml"
    overrides = load_side_overrides(overrides_path)

    def _has_override(entry: dict) -> bool:
        mid = str(entry.get("id", ""))
        fname = str(entry.get("file", ""))
        if mid and mid in overrides.by_id:
            return True
        return bool(fname and (fname in overrides.by_filename or fname in overrides.deployment_tool_review))

    marked = [e for e in entries if (not _is_unmarked(e)) or _has_override(e)]
    dropped = len(entries) - len(marked)
    logger.debug(f"_resolve_client_mod_sources: {len(entries)} entr(ies); {dropped} dropped as unmarked (no override)")

    if not overrides.is_empty():
        marked = apply_side_overrides(marked, overrides)
        logger.debug("_resolve_client_mod_sources: side overrides applied")

    side_entries = deps.filter_prism_entries_by_side(marked, "client")
    logger.debug(f"_resolve_client_mod_sources: client side filter -> {len(side_entries)} seed entr(ies)")

    closure = deps.expand_with_required(
        all_entries=marked,
        seed_entries=side_entries,
        target_side="client",
        modpack_dir=config.modpack_dir,
        logger=logger,
    )

    out: dict[str, Path] = {}
    for entry in closure.entries:
        filename = entry.get("file")
        if not filename:
            continue
        path = config.modpack_dir / filename
        if not path.is_file():
            logger.warning(f"client mod source missing from disk: {filename}")
            continue
        out[str(filename)] = path

    logger.info(f"_resolve_client_mod_sources: {len(out)} client-side mod source(s) ({len(closure.entries)} in closure)")
    return out


# ---------------------------------------------------------------------------
# Staging assembly
# ---------------------------------------------------------------------------


def _stage_client_mods(config: DeploymentConfig, staging_dir: Path, logger: Any) -> None:
    """Copy the client mod set into ``staging/mods/``."""
    mods_dir = staging_dir / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    sources = _resolve_client_mod_sources(config, logger)
    copied = 0
    for name, src in sources.items():
        try:
            shutil.copy2(src, mods_dir / name)
            copied += 1
        except OSError as exc:
            logger.warning(f"staging: copy mod {name} failed: {exc}")
    logger.debug(f"staging: {copied}/{len(sources)} mod(s) copied to {mods_dir}")


def _stage_sync_mapping(config: DeploymentConfig, staging_dir: Path, logger: Any) -> None:
    """Copy each non-shared, non-resourcepack sync item into staging.

    The resourcepacks key is handled by :func:`_stage_resource_packs`.
    Shared destinations (``@www/...``) are skipped; they're published
    separately, not embedded in the ZIP.
    """
    for key, mapping_value in config.sync_mapping.items():
        if key == "resourcepacks":
            logger.debug("staging: key 'resourcepacks' handled by _stage_resource_packs; skipping")
            continue
        dest_rel = resolve_mapping_for_side(mapping_value, "client")
        if dest_rel is None:
            logger.debug(f"staging: {key} excluded on client side; skipping")
            continue
        if is_shared_dest(dest_rel):
            logger.debug(f"staging: {key} shared dest {dest_rel!r} published separately; skipping")
            continue
        src = config.sync_root / key
        if not src.is_dir():
            logger.debug(f"staging: {key} source {src} absent; skipping")
            continue
        dst = staging_dir / dest_rel
        logger.debug(f"staging: {key} {src} -> {dst} (merge)")
        copy_tree(src, dst, mode="merge", logger=logger)


def _stage_resource_packs(config: DeploymentConfig, staging_dir: Path, logger: Any) -> None:
    """Stage the union of ``[resource_pack.X].filename`` values (§7.6).

    Source path resolution: ``sync_root / resourcepacks.client``. Only
    files that exist on disk are staged; preflight validates existence
    (§7.8) so a missing source at this point is a defensive warning.
    """
    rp_mapping = config.sync_mapping.get("resourcepacks")
    if not isinstance(rp_mapping, dict):
        logger.debug("staging: no [sync_mapping].resourcepacks; skipping RP staging")
        return
    client_sub = rp_mapping.get("client")
    if not isinstance(client_sub, str) or not client_sub:
        logger.debug("staging: [sync_mapping].resourcepacks.client not set; skipping RP staging")
        return

    source_dir = config.sync_root / client_sub
    filenames: list[str] = []
    seen: set[str] = set()
    for rp in config.resource_packs.values():
        if rp.filename in seen:
            continue
        seen.add(rp.filename)
        filenames.append(rp.filename)
    if not filenames:
        logger.debug("staging: no resource_pack.X.filename values; skipping RP staging")
        return

    dest_dir = staging_dir / "resourcepacks"
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in filenames:
        src = source_dir / name
        if not src.is_file():
            logger.warning(f"client staging: RP source missing: {src}")
            continue
        try:
            shutil.copy2(src, dest_dir / name)
            copied += 1
        except OSError as exc:
            logger.warning(f"client staging: copy RP {name} failed: {exc}")
    logger.debug(f"staging: {copied}/{len(filenames)} resource pack(s) copied to {dest_dir}")


def _build_staging(
    config: DeploymentConfig,
    with_resources: bool,
    staging_dir: Path,
    logger: Any,
) -> None:
    """Assemble the client staging tree under ``staging_dir``."""
    logger.debug(f"staging: building into {staging_dir} (with_resources={with_resources})")
    _stage_client_mods(config, staging_dir, logger)
    _stage_sync_mapping(config, staging_dir, logger)
    if with_resources:
        _stage_resource_packs(config, staging_dir, logger)
    else:
        logger.debug("staging: --with-resources not set; skipping resource pack staging")


# ---------------------------------------------------------------------------
# @www shared-item publication (§2.1)
# ---------------------------------------------------------------------------


def _shared_dest_of(value: Any) -> str | None:
    """Return the ``@www/...`` destination of a sync-mapping value, if any.

    Only plain string values are considered. A dict value with a
    ``resource_pack`` key is scope_resource_pack's concern (§4.9).
    """
    if isinstance(value, str) and value.startswith("@www/"):
        return value
    return None


def _publish_shared_items(
    config: DeploymentConfig,
    protect_patterns: list[str],
    logger: Any,
) -> tuple[list[Path], dict[str, CopyResult]]:
    """Copy each sync_mapping entry whose value is ``@www/...`` to www_dir.

    Source: ``sync_root / key``. Destination: resolved via
    ``files.resolve_shared_dest``. Uses merge semantics.
    """
    published: list[Path] = []
    results: dict[str, CopyResult] = {}
    if config.www_dir is None:
        logger.debug("@www publish: www_dir is None; nothing to publish")
        return published, results

    for key, mapping_value in config.sync_mapping.items():
        dest_value = _shared_dest_of(mapping_value)
        if dest_value is None:
            continue
        src = config.sync_root / key
        if not src.is_dir():
            logger.debug(f"@www publish: {key} source {src} absent; skipping")
            continue
        try:
            dest_dir = resolve_shared_dest(dest_value, config.www_dir)
        except Exception as exc:
            logger.warning(f"@www publish: bad destination {dest_value!r}: {exc}")
            continue
        logger.debug(f"@www publish: {key} {src} -> {dest_dir} (merge)")
        result = copy_tree(src, dest_dir, mode="merge", logger=logger)
        published.append(dest_dir)
        results[key] = result
        logger.info(f"@www publish: {src} -> {dest_dir} ({result.changed_count} change(s))")
    return published, results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def deploy_client_scope(
    config: DeploymentConfig,
    with_resources: bool,
    protect_patterns: list[str],
    logger: Any = None,
) -> ClientScopeResult:
    """Execute the client scope write phase (§4.2, §7.1).

    Halts on the first runtime failure. Returns a result describing
    what was written. Never raises on a write failure; the caller
    decides exit code and recovery.

    Read-only with respect to Docker: no container operations happen
    here.
    """
    if logger is None:
        logger = _log

    logger.info(f"client scope: entering; output_filename={config.output_filename!r} www_dir={config.www_dir} with_resources={with_resources}")

    result = ClientScopeResult()

    if config.www_dir is None:
        result.success = False
        result.failure_message = "www_dir is not set; client scope requires it"
        logger.error(f"client scope: {result.failure_message}")
        return result

    # ------------------------------------------------------------------
    # 1. Resolve output names
    # ------------------------------------------------------------------
    resolved_name, date_str = _resolve_output_name(config.output_filename, logger)
    result.resolved_output_filename = resolved_name
    changelog_name = _changelog_name(resolved_name, logger)
    base = config.download_base_url.rstrip("/") if config.download_base_url else ""
    logger.debug(f"client scope: resolved output name={resolved_name!r} changelog name={changelog_name!r} date={date_str} base={base!r}")

    # ------------------------------------------------------------------
    # 2. Assemble staging tree
    # ------------------------------------------------------------------
    staging_root = Path(tempfile.mkdtemp(prefix="deploy_client_"))
    logger.debug(f"client scope: staging root {staging_root}")
    try:
        try:
            _build_staging(config, with_resources, staging_root, logger)
        except Exception as exc:
            result.success = False
            result.failure_message = f"staging assembly failed: {exc}"
            logger.error(f"client scope: {result.failure_message}")
            return result

        # ------------------------------------------------------------------
        # 3. Find baseline BEFORE writing the new ZIP (§7.1)
        # ------------------------------------------------------------------
        baseline = _find_baseline(config.www_dir, config.output_filename, date_str, logger)
        result.baseline_zip = baseline
        if baseline is not None:
            logger.info(f"client scope: changelog baseline {baseline}")
        else:
            logger.info("client scope: no baseline found; this will be an initial build")

        # ------------------------------------------------------------------
        # 4. Diff report (§1.1)
        # ------------------------------------------------------------------
        try:
            report = changelog_mod.build_client_diff_report(
                staging_dir=staging_root,
                previous_zip=baseline,
                logger=logger,
            )
        except Exception as exc:
            result.success = False
            result.failure_message = f"changelog diff failed: {exc}"
            logger.error(f"client scope: {result.failure_message}")
            return result
        result.report = report
        result.initial_build = report.initial_build
        logger.debug(
            f"client scope: diff report; initial_build={report.initial_build} "
            f"added_mods={len(report.added_mods)} removed_mods={len(report.removed_mods)} "
            f"kubejs_added={len(report.added_kubejs)} "
            f"kubejs_modified={len(report.modified_kubejs)} "
            f"kubejs_removed={len(report.removed_kubejs)}"
        )

        # ------------------------------------------------------------------
        # 5. Create ZIP (atomic, §4.10)
        # ------------------------------------------------------------------
        zip_path = config.www_dir / resolved_name
        logger.info(f"client scope: creating ZIP {zip_path}")
        try:
            create_zip(staging_root, zip_path, logger=logger)
        except Exception as exc:
            result.success = False
            result.failure_message = f"ZIP creation failed: {exc}"
            logger.error(f"client scope: {result.failure_message}")
            return result
        result.zip_path = zip_path
        result.zip_url = f"{base}/{resolved_name}" if base else resolved_name
        logger.debug(f"client scope: ZIP url {result.zip_url}")

        # ------------------------------------------------------------------
        # 6. Hash the ZIP, write changelog HTML (§7.1)
        # ------------------------------------------------------------------
        try:
            result.zip_sha256 = compute_sha256(zip_path)
            logger.info(f"client scope: ZIP sha256 {result.zip_sha256}")
        except OSError as exc:
            logger.warning(f"client scope: could not hash {zip_path}: {exc}")
            result.zip_sha256 = "unavailable"

        timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
        changelog_path = config.www_dir / changelog_name
        logger.debug(f"client scope: writing changelog HTML {changelog_path}")
        try:
            html_text = changelog_mod.render_changelog_html(
                report=report,
                artifact_name=resolved_name,
                artifact_url=result.zip_url,
                sha256sum=result.zip_sha256,
                timestamp=timestamp,
            )
            atomic_write(
                changelog_path,
                html_text.encode("utf-8"),
                logger=logger,
            )
        except Exception as exc:
            result.success = False
            result.failure_message = f"changelog HTML write failed: {exc}"
            logger.error(f"client scope: {result.failure_message}")
            return result
        result.changelog_path = changelog_path
        result.changelog_url = f"{base}/{changelog_name}" if base else changelog_name
        logger.info(f"client scope: changelog HTML published to {changelog_path}")

        # ------------------------------------------------------------------
        # 7. Publish @www shared items (§2.1)
        # ------------------------------------------------------------------
        try:
            published, shared_results = _publish_shared_items(config, protect_patterns, logger)
        except Exception as exc:
            result.success = False
            result.failure_message = f"@www shared-item publication failed: {exc}"
            logger.error(f"client scope: {result.failure_message}")
            return result
        result.published_shared = published
        result.shared_results = shared_results
        if published:
            logger.info(f"client scope: {len(published)} @www shared item(s) published")

    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
        logger.debug(f"client scope: staging root {staging_root} removed")

    logger.info(f"client scope: complete; zip={result.zip_path} initial_build={result.initial_build} shared_items={len(result.published_shared)}")
    return result
