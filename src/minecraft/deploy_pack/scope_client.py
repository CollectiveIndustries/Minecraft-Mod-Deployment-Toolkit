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

Structure
---------

The module is organised into six sections with explicit banner
comments. Each section corresponds to one concern and its helpers are
only called from within that section or from the public entry point.

    §1  Result dataclass
    §2  Output filename resolution + baseline (§7.1)
    §3  Client mod source resolution
    §4  Staging assembly
    §5  @www shared-item publication (§2.1)
    §6  Entry point

The orchestrator :func:`deploy_client_scope` runs five independent
phases. Each phase mutates ``result`` in place and returns either
``None`` to continue or a failure message to abort. The
:func:`_run_phases` helper iterates them, so the orchestrator stays a
linear "resolve names, run phases, cleanup" walk. The staging temp
directory is created before the phases and unconditionally removed in
a ``finally`` block, regardless of which phase aborted.

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
from collections.abc import Callable
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


# ===========================================================================
# §1  Result dataclass
# ===========================================================================


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


# ===========================================================================
# §2  Output filename resolution + baseline (§7.1)
# ===========================================================================


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


def _baseline_without_date_token(output_dir: Path, template: str, logger: Any) -> Path | None:
    """Case 1 of §7.1: no ``{date}`` in the template.

    The baseline is the existing file at the resolved name, or None.
    """
    candidate = output_dir / template
    if candidate.is_file():
        logger.debug(f"_find_baseline: no-date template; using existing {candidate}")
        return candidate
    logger.debug(f"_find_baseline: no-date template; {candidate} does not exist")
    return None


def _glob_baseline_candidates(
    output_dir: Path,
    prefix: str,
    suffix: str,
    logger: Any,
) -> list[tuple[str, Path]]:
    """Glob ``<prefix>*<suffix>`` under ``output_dir``; return ``(middle, path)`` pairs.

    ``middle`` is the substring between the two affixes, which for a
    well-formed filename is the date. Entries whose middle is empty or
    which are not regular files are skipped.
    """
    pattern = f"{prefix}*{suffix}"
    candidates: list[tuple[str, Path]] = []
    if not output_dir.is_dir():
        return candidates
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
    logger.debug(f"_find_baseline: pattern={pattern!r} -> {len(candidates)} candidate(s)")
    return candidates


def _pick_same_date_or_latest(
    candidates: list[tuple[str, Path]],
    date_str: str,
    logger: Any,
) -> Path | None:
    """Prefer a same-date candidate; otherwise the lexicographic-max date."""
    for middle, p in candidates:
        if middle == date_str:
            logger.debug(f"_find_baseline: same-date baseline {p}")
            return p
    candidates.sort(key=lambda x: x[0], reverse=True)
    chosen = candidates[0][1]
    logger.debug(f"_find_baseline: most-recent prior baseline {chosen}")
    return chosen


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
        return _baseline_without_date_token(output_dir, template, logger)

    prefix, _, suffix = template.partition(_DATE_TOKEN)
    if not prefix and not suffix:
        logger.debug("_find_baseline: template is only {date}; cannot resolve")
        return None

    candidates = _glob_baseline_candidates(output_dir, prefix, suffix, logger)
    if not candidates:
        logger.debug(f"_find_baseline: no candidates matching {prefix!r}*{suffix!r} in {output_dir}")
        return None
    return _pick_same_date_or_latest(candidates, date_str, logger)


# ===========================================================================
# §3  Client mod source resolution
# ===========================================================================


def _is_unmarked(entry: dict) -> bool:
    """§6.3: an entry whose declared ``side`` is outside the valid set.

    ``side_raw is None`` (no ``side`` key in the .pw.toml) is treated as
    marked, matching the parser's default of ``"both"``.
    """
    raw = entry.get("side_raw")
    if raw is None:
        return False
    return raw not in ("client", "server", "both")


def _has_override(entry: dict, overrides: Any) -> bool:
    """Return True if any override section matches this entry (§3.11)."""
    mid = str(entry.get("id", ""))
    fname = str(entry.get("file", ""))
    if mid and mid in overrides.by_id:
        return True
    return bool(fname and (fname in overrides.by_filename or fname in overrides.deployment_tool_review))


def _filter_marked(entries: list[dict], overrides: Any, logger: Any) -> list[dict]:
    """Drop unmarked entries that have no override; log the drop count."""
    marked = [e for e in entries if (not _is_unmarked(e)) or _has_override(e, overrides)]
    dropped = len(entries) - len(marked)
    logger.debug(f"_resolve_client_mod_sources: {len(entries)} entr(ies); {dropped} dropped as unmarked (no override)")
    return marked


def _collect_existing_mod_paths(entries: list[dict], modpack_dir: Path, logger: Any) -> dict[str, Path]:
    """Return ``{filename: path}`` for every entry present on disk.

    Missing files log at WARN and are skipped.
    """
    out: dict[str, Path] = {}
    for entry in entries:
        filename = entry.get("file")
        if not filename:
            continue
        path = modpack_dir / filename
        if not path.is_file():
            logger.warning(f"client mod source missing from disk: {filename}")
            continue
        out[str(filename)] = path
    logger.info(f"_resolve_client_mod_sources: {len(out)} client-side mod source(s) ({len(entries)} in closure)")
    return out


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

    overrides = load_side_overrides(config.config_dir / "side_overrides.toml")
    marked = _filter_marked(entries, overrides, logger)
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
    return _collect_existing_mod_paths(closure.entries, config.modpack_dir, logger)


# ===========================================================================
# §4  Staging assembly
# ===========================================================================


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


def _stage_one_sync_key(
    config: DeploymentConfig,
    staging_dir: Path,
    key: str,
    mapping_value: Any,
    logger: Any,
) -> None:
    """Copy one non-shared, non-resourcepack sync item into staging.

    Skips keys excluded on the client side and keys whose destination is
    a shared (``@www/...``) location; those are published separately, not
    embedded in the ZIP.
    """
    dest_rel = resolve_mapping_for_side(mapping_value, "client")
    if dest_rel is None:
        logger.debug(f"staging: {key} excluded on client side; skipping")
        return
    if is_shared_dest(dest_rel):
        logger.debug(f"staging: {key} shared dest {dest_rel!r} published separately; skipping")
        return
    src = config.sync_root / key
    if not src.is_dir():
        logger.debug(f"staging: {key} source {src} absent; skipping")
        return
    dst = staging_dir / dest_rel
    logger.debug(f"staging: {key} {src} -> {dst} (merge)")
    copy_tree(src, dst, mode="merge", logger=logger)


def _stage_sync_mapping(config: DeploymentConfig, staging_dir: Path, logger: Any) -> None:
    """Copy each non-shared, non-resourcepack sync item into staging.

    The resourcepacks key is handled by :func:`_stage_resource_packs`.
    """
    for key, mapping_value in config.sync_mapping.items():
        if key == "resourcepacks":
            logger.debug("staging: key 'resourcepacks' handled by _stage_resource_packs; skipping")
            continue
        _stage_one_sync_key(config, staging_dir, key, mapping_value, logger)


def _collect_rp_filenames(config: DeploymentConfig) -> list[str]:
    """Return deduplicated ``[resource_pack.X].filename`` values (§7.6)."""
    filenames: list[str] = []
    seen: set[str] = set()
    for rp in config.resource_packs.values():
        if rp.filename in seen:
            continue
        seen.add(rp.filename)
        filenames.append(rp.filename)
    return filenames


def _copy_rp_files(
    source_dir: Path,
    dest_dir: Path,
    filenames: list[str],
    logger: Any,
) -> int:
    """Copy each filename from ``source_dir`` into ``dest_dir``; return the count.

    Missing sources log at WARN and are skipped.
    """
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
    return copied


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

    filenames = _collect_rp_filenames(config)
    if not filenames:
        logger.debug("staging: no resource_pack.X.filename values; skipping RP staging")
        return

    source_dir = config.sync_root / client_sub
    dest_dir = staging_dir / "resourcepacks"
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = _copy_rp_files(source_dir, dest_dir, filenames, logger)
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


# ===========================================================================
# §5  @www shared-item publication (§2.1)
# ===========================================================================


def _shared_dest_of(value: Any) -> str | None:
    """Return the ``@www/...`` destination of a sync-mapping value, if any.

    Only plain string values are considered. A dict value with a
    ``resource_pack`` key is scope_resource_pack's concern (§4.9).
    """
    if isinstance(value, str) and value.startswith("@www/"):
        return value
    return None


def _publish_one_shared_item(
    config: DeploymentConfig,
    key: str,
    dest_value: str,
    logger: Any,
) -> tuple[Path, CopyResult] | None:
    """Publish one ``@www/...`` entry.

    Returns ``(dest_dir, result)`` on success, or None when the source
    is absent or the destination is malformed. Source: ``sync_root/key``.
    """
    src = config.sync_root / key
    if not src.is_dir():
        logger.debug(f"@www publish: {key} source {src} absent; skipping")
        return None
    try:
        dest_dir = resolve_shared_dest(dest_value, config.www_dir)
    except Exception as exc:
        logger.warning(f"@www publish: bad destination {dest_value!r}: {exc}")
        return None
    logger.debug(f"@www publish: {key} {src} -> {dest_dir} (merge)")
    result = copy_tree(src, dest_dir, mode="merge", logger=logger)
    logger.info(f"@www publish: {src} -> {dest_dir} ({result.changed_count} change(s))")
    return (dest_dir, result)


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
        outcome = _publish_one_shared_item(config, key, dest_value, logger)
        if outcome is None:
            continue
        dest_dir, result = outcome
        published.append(dest_dir)
        results[key] = result
    return published, results


# ===========================================================================
# §6  Entry point
# ===========================================================================


def _run_phases(phases: list[Callable[[], str | None]], logger: Any) -> str | None:
    """Run each phase callable in order; return the first error message or None.

    Each phase performs its work (mutating a shared result object) and
    returns ``None`` to continue or a failure message to abort. The
    caller converts a non-None result into ``result.success = False``
    plus ``result.failure_message``.
    """
    for phase in phases:
        err = phase()
        if err is not None:
            return err
    return None


def _phase_stage(
    config: DeploymentConfig,
    with_resources: bool,
    staging_root: Path,
    logger: Any,
) -> str | None:
    """Build the staging tree; on failure, return the error message."""
    try:
        _build_staging(config, with_resources, staging_root, logger)
    except Exception as exc:
        msg = f"staging assembly failed: {exc}"
        logger.error(f"client scope: {msg}")
        return msg
    return None


def _phase_baseline_and_diff(
    config: DeploymentConfig,
    staging_root: Path,
    date_str: str,
    result: ClientScopeResult,
    logger: Any,
) -> str | None:
    """Resolve the baseline and build the diff report."""
    baseline = _find_baseline(config.www_dir, config.output_filename, date_str, logger)
    result.baseline_zip = baseline
    if baseline is not None:
        logger.info(f"client scope: changelog baseline {baseline}")
    else:
        logger.info("client scope: no baseline found; this will be an initial build")

    try:
        report = changelog_mod.build_client_diff_report(
            staging_dir=staging_root,
            previous_zip=baseline,
            logger=logger,
        )
    except Exception as exc:
        msg = f"changelog diff failed: {exc}"
        logger.error(f"client scope: {msg}")
        return msg
    result.report = report
    result.initial_build = report.initial_build
    logger.debug(
        f"client scope: diff report; initial_build={report.initial_build} "
        f"added_mods={len(report.added_mods)} removed_mods={len(report.removed_mods)} "
        f"kubejs_added={len(report.added_kubejs)} "
        f"kubejs_modified={len(report.modified_kubejs)} "
        f"kubejs_removed={len(report.removed_kubejs)}"
    )
    return None


def _phase_zip(
    config: DeploymentConfig,
    resolved_name: str,
    base: str,
    staging_root: Path,
    result: ClientScopeResult,
    logger: Any,
) -> str | None:
    """Create the client ZIP and record its path and URL on ``result``."""
    zip_path = config.www_dir / resolved_name
    logger.info(f"client scope: creating ZIP {zip_path}")
    try:
        create_zip(staging_root, zip_path, logger=logger)
    except Exception as exc:
        msg = f"ZIP creation failed: {exc}"
        logger.error(f"client scope: {msg}")
        return msg
    result.zip_path = zip_path
    result.zip_url = f"{base}/{resolved_name}" if base else resolved_name
    logger.debug(f"client scope: ZIP url {result.zip_url}")
    return None


def _phase_changelog(
    config: DeploymentConfig,
    changelog_name: str,
    base: str,
    result: ClientScopeResult,
    logger: Any,
) -> str | None:
    """Hash the ZIP (best-effort) and write the changelog HTML."""
    try:
        result.zip_sha256 = compute_sha256(result.zip_path)
        logger.info(f"client scope: ZIP sha256 {result.zip_sha256}")
    except OSError as exc:
        logger.warning(f"client scope: could not hash {result.zip_path}: {exc}")
        result.zip_sha256 = "unavailable"

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
    changelog_path = config.www_dir / changelog_name
    logger.debug(f"client scope: writing changelog HTML {changelog_path}")
    try:
        html_text = changelog_mod.render_changelog_html(
            report=result.report,
            artifact_name=result.resolved_output_filename,
            artifact_url=result.zip_url,
            sha256sum=result.zip_sha256,
            timestamp=timestamp,
        )
        atomic_write(changelog_path, html_text.encode("utf-8"), logger=logger)
    except Exception as exc:
        msg = f"changelog HTML write failed: {exc}"
        logger.error(f"client scope: {msg}")
        return msg
    result.changelog_path = changelog_path
    result.changelog_url = f"{base}/{changelog_name}" if base else changelog_name
    logger.info(f"client scope: changelog HTML published to {changelog_path}")
    return None


def _phase_shared(
    config: DeploymentConfig,
    protect_patterns: list[str],
    result: ClientScopeResult,
    logger: Any,
) -> str | None:
    """Publish @www shared items and record them on ``result``."""
    try:
        published, shared_results = _publish_shared_items(config, protect_patterns, logger)
    except Exception as exc:
        msg = f"@www shared-item publication failed: {exc}"
        logger.error(f"client scope: {msg}")
        return msg
    result.published_shared = published
    result.shared_results = shared_results
    if published:
        logger.info(f"client scope: {len(published)} @www shared item(s) published")
    return None


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

    resolved_name, date_str = _resolve_output_name(config.output_filename, logger)
    result.resolved_output_filename = resolved_name
    changelog_name = _changelog_name(resolved_name, logger)
    base = config.download_base_url.rstrip("/") if config.download_base_url else ""
    logger.debug(f"client scope: resolved output name={resolved_name!r} changelog name={changelog_name!r} date={date_str} base={base!r}")

    staging_root = Path(tempfile.mkdtemp(prefix="deploy_client_"))
    logger.debug(f"client scope: staging root {staging_root}")
    try:
        err = _run_phases(
            [
                lambda: _phase_stage(config, with_resources, staging_root, logger),
                lambda: _phase_baseline_and_diff(config, staging_root, date_str, result, logger),
                lambda: _phase_zip(config, resolved_name, base, staging_root, result, logger),
                lambda: _phase_changelog(config, changelog_name, base, result, logger),
                lambda: _phase_shared(config, protect_patterns, result, logger),
            ],
            logger,
        )
        if err is not None:
            result.success = False
            result.failure_message = err
            return result
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
        logger.debug(f"client scope: staging root {staging_root} removed")

    logger.info(f"client scope: complete; zip={result.zip_path} initial_build={result.initial_build} shared_items={len(result.published_shared)}")
    return result
