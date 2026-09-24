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
"""

from __future__ import annotations

import datetime
import logging
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
from .overrides import apply_side_overrides, load_side_overrides

__all__ = [
    "ClientScopeResult",
    "deploy_client_scope",
]


_log = logging.getLogger(__name__)


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


def _resolve_output_name(template: str) -> tuple[str, str]:
    """Return ``(resolved_filename, date_str)``.

    ``{date}`` is replaced with today's UTC date in YYYYMMDD form. A
    template without the token is returned unchanged (per §7.1's
    "same-date re-run when output_filename does not contain {date}"
    clause).
    """
    date_str = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")
    return (template.replace(_DATE_TOKEN, date_str), date_str)


def _changelog_name(resolved_zip_name: str) -> str:
    """§7.1: replace the trailing ``.zip`` with ``.html``."""
    if resolved_zip_name.endswith(".zip"):
        return resolved_zip_name[: -len(".zip")] + ".html"
    return resolved_zip_name + ".html"


def _find_baseline(output_dir: Path, template: str, date_str: str) -> Path | None:
    """Find the changelog baseline ZIP per §7.1.

    Cases:

      1. Template has no ``{date}``: baseline = existing file at the
         resolved name (or None).
      2. Template has ``{date}``: look for ZIPs matching
         ``<prefix>*<suffix>``; prefer the same-date candidate if
         present; else the lexicographic maximum of the date substring.

    Only files whose inter-token portion is non-empty are considered.
    """
    if _DATE_TOKEN not in template:
        candidate = output_dir / template
        return candidate if candidate.is_file() else None

    prefix, _, suffix = template.partition(_DATE_TOKEN)
    if not prefix and not suffix:
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
        return None

    for middle, p in candidates:
        if middle == date_str:
            return p

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# Mod source resolution
# ---------------------------------------------------------------------------


def _is_unmarked(entry: dict) -> bool:
    """§6.3: no ``side`` key is marked (default both).

    An invalid raw value is unmarked.
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
        return {}
    entries = deps.load_prism_index(index_dir)
    if not entries:
        return {}

    overrides_path = config.config_dir / "side_overrides.toml"
    overrides = load_side_overrides(overrides_path)

    # Determine which entries have a review or explicit override. The
    # precedence order matches overrides.py's lookup().
    def _has_override(entry: dict) -> bool:
        mid = str(entry.get("id", ""))
        fname = str(entry.get("file", ""))
        if mid and mid in overrides.by_id:
            return True
        return bool(fname and (fname in overrides.by_filename or fname in overrides.deployment_tool_review))

    marked = [e for e in entries if (not _is_unmarked(e)) or _has_override(e)]

    if not overrides.is_empty():
        marked = apply_side_overrides(marked, overrides)

    side_entries = deps.filter_prism_entries_by_side(marked, "client")
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
            if logger is not None:
                logger.warning(f"client mod source missing from disk: {filename}")
            continue
        out[str(filename)] = path
    return out


# ---------------------------------------------------------------------------
# Staging assembly
# ---------------------------------------------------------------------------


def _stage_client_mods(config: DeploymentConfig, staging_dir: Path, logger: Any) -> None:
    mods_dir = staging_dir / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    sources = _resolve_client_mod_sources(config, logger)
    for name, src in sources.items():
        try:
            shutil.copy2(src, mods_dir / name)
        except OSError as exc:
            if logger is not None:
                logger.warning(f"staging: copy mod {name} failed: {exc}")


def _stage_sync_mapping(config: DeploymentConfig, staging_dir: Path, logger: Any) -> None:
    """Copy each non-shared, non-resourcepack sync item into staging.

    The resourcepacks key is handled by :func:`_stage_resource_packs`.
    Shared destinations (``@www/...``) are skipped; they're published
    separately, not embedded in the ZIP.
    """
    for key, mapping_value in config.sync_mapping.items():
        if key == "resourcepacks":
            continue
        dest_rel = resolve_mapping_for_side(mapping_value, "client")
        if dest_rel is None:
            continue
        if is_shared_dest(dest_rel):
            continue
        src = config.sync_root / key
        if not src.is_dir():
            if logger is not None:
                logger.debug(f"client staging: source {src} absent; skipping {key}")
            continue
        dst = staging_dir / dest_rel
        copy_tree(src, dst, mode="merge", logger=logger)


def _stage_resource_packs(config: DeploymentConfig, staging_dir: Path, logger: Any) -> None:
    """Stage the union of ``[resource_pack.X].filename`` values (§7.6).

    Source path resolution: ``sync_root / resourcepacks.client``. Only
    files that exist on disk are staged; preflight validates existence
    (§7.8) so a missing source at this point is a defensive warning.
    """
    rp_mapping = config.sync_mapping.get("resourcepacks")
    if not isinstance(rp_mapping, dict):
        return
    client_sub = rp_mapping.get("client")
    if not isinstance(client_sub, str) or not client_sub:
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
        return

    dest_dir = staging_dir / "resourcepacks"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in filenames:
        src = source_dir / name
        if not src.is_file():
            if logger is not None:
                logger.warning(f"client staging: RP source missing: {src}")
            continue
        try:
            shutil.copy2(src, dest_dir / name)
        except OSError as exc:
            if logger is not None:
                logger.warning(f"client staging: copy RP {name} failed: {exc}")


def _build_staging(
    config: DeploymentConfig,
    with_resources: bool,
    staging_dir: Path,
    logger: Any,
) -> None:
    _stage_client_mods(config, staging_dir, logger)
    _stage_sync_mapping(config, staging_dir, logger)
    if with_resources:
        _stage_resource_packs(config, staging_dir, logger)


# ---------------------------------------------------------------------------
# @www shared-item publication (§2.1)
# ---------------------------------------------------------------------------


def _shared_dest_of(value: Any) -> str | None:
    """Return the ``@www/...`` destination of a sync-mapping value, if any.

    Only plain string values are considered. A dict value with a
    ``resource_pack`` key is scope_resource_pack's concern (§4.9), not
    this scope's.
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
    ``files.resolve_shared_dest``. Uses merge semantics (§7.2's
    config/kubejs-specific modes do not apply to arbitrary shared items).
    """
    published: list[Path] = []
    results: dict[str, CopyResult] = {}
    if config.www_dir is None:
        return published, results

    for key, mapping_value in config.sync_mapping.items():
        dest_value = _shared_dest_of(mapping_value)
        if dest_value is None:
            continue
        src = config.sync_root / key
        if not src.is_dir():
            if logger is not None:
                logger.debug(f"@www publish: source {src} absent; skipping {key}")
            continue
        try:
            dest_dir = resolve_shared_dest(dest_value, config.www_dir)
        except Exception as exc:
            if logger is not None:
                logger.warning(f"@www publish: bad destination {dest_value!r}: {exc}")
            continue
        result = copy_tree(src, dest_dir, mode="merge", logger=logger)
        published.append(dest_dir)
        results[key] = result
        if logger is not None:
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
    result = ClientScopeResult()

    if config.www_dir is None:
        result.success = False
        result.failure_message = "www_dir is not set; client scope requires it"
        return result

    # ------------------------------------------------------------------
    # 1. Resolve output names
    # ------------------------------------------------------------------
    resolved_name, date_str = _resolve_output_name(config.output_filename)
    result.resolved_output_filename = resolved_name
    changelog_name = _changelog_name(resolved_name)
    base = config.download_base_url.rstrip("/") if config.download_base_url else ""

    # ------------------------------------------------------------------
    # 2. Assemble staging tree
    # ------------------------------------------------------------------
    staging_root = Path(tempfile.mkdtemp(prefix="deploy_client_"))
    try:
        try:
            _build_staging(config, with_resources, staging_root, logger)
        except Exception as exc:
            result.success = False
            result.failure_message = f"staging assembly failed: {exc}"
            return result

        # ------------------------------------------------------------------
        # 3. Find baseline BEFORE writing the new ZIP (§7.1)
        # ------------------------------------------------------------------
        baseline = _find_baseline(config.www_dir, config.output_filename, date_str)
        result.baseline_zip = baseline

        # ------------------------------------------------------------------
        # 4. Diff report (§1.1)
        # ------------------------------------------------------------------
        try:
            report = changelog_mod.build_client_diff_report(
                staging_dir=staging_root,
                previous_zip=baseline,
                logger=logger if logger is not None else _log,
            )
        except Exception as exc:
            result.success = False
            result.failure_message = f"changelog diff failed: {exc}"
            return result
        result.report = report
        result.initial_build = report.initial_build

        # ------------------------------------------------------------------
        # 5. Create ZIP (atomic, §4.10)
        # ------------------------------------------------------------------
        zip_path = config.www_dir / resolved_name
        try:
            create_zip(
                staging_root,
                zip_path,
                logger=logger if logger is not None else _log,
            )
        except Exception as exc:
            result.success = False
            result.failure_message = f"ZIP creation failed: {exc}"
            return result
        result.zip_path = zip_path
        result.zip_url = f"{base}/{resolved_name}" if base else resolved_name

        # ------------------------------------------------------------------
        # 6. Hash the ZIP, write changelog HTML (§7.1)
        # ------------------------------------------------------------------
        try:
            result.zip_sha256 = compute_sha256(zip_path)
        except OSError as exc:
            if logger is not None:
                logger.warning(f"could not hash {zip_path}: {exc}")
            result.zip_sha256 = "unavailable"

        timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
        changelog_path = config.www_dir / changelog_name
        try:
            # changelog.write_changelog writes directly (no atomic
            # helper). Route through atomic_write by rendering first.
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
                logger=logger if logger is not None else _log,
            )
        except Exception as exc:
            result.success = False
            result.failure_message = f"changelog HTML write failed: {exc}"
            return result
        result.changelog_path = changelog_path
        result.changelog_url = f"{base}/{changelog_name}" if base else changelog_name

        # ------------------------------------------------------------------
        # 7. Publish @www shared items (§2.1)
        # ------------------------------------------------------------------
        try:
            published, shared_results = _publish_shared_items(config, protect_patterns, logger)
        except Exception as exc:
            result.success = False
            result.failure_message = f"@www shared-item publication failed: {exc}"
            return result
        result.published_shared = published
        result.shared_results = shared_results

    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    return result
