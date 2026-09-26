# src/minecraft/common/changelog.py

"""Changelog generation: diff the current pack and render HTML.

The page describes the CLIENT PACK - what changed in it since the last
build. The page is titled after the client ZIP, and the ZIP is what
users download, so the diff must be about the ZIP's contents.

Baseline is the most recent prior client ZIP in the same directory,
read before the current ZIP is written. On a first build with no
previous ZIP, the page reports the initial contents rather than an
empty diff.

The page lists:

  - mods added (in the new ZIP, not in the previous)
  - mods removed (in the previous, not in the new)
  - KubeJS files added / modified / removed (by content hash)

Mods are compared by filename only. A version bump produces a new
filename, so it appears as one addition and one removal - an honest
description of what the user sees.

No network calls, no LLM, no git. Output is deterministic and can be
diffed across builds.

Logging
-------

Module logger is ``minecraft.common.changelog``. The module is a
read-only diff-and-render pipeline with no failure modes of its own:
every error comes from a bad ZIP or an unreadable staging file, and
those propagate to the caller (``scope_client.deploy_client_scope``)
which wraps them at ERROR. There are therefore no ERROR sites here.

``build_client_diff_report`` emits exactly one INFO line per run: the
``summary_line`` teaser. Everything else it records - the entry
context, the staging-mods count, the initial-vs-diff branch - logs at
DEBUG so a normal deploy shows a single changelog line. Its per-stage
DEBUG lines cover the phases (mod hashing, KubeJS hashing). The two
private ZIP readers (``_zip_names``, ``_zip_hashes``) and the
staging-tree walker (``_staging_hashes``) log entry paths and result
counts at DEBUG. ``compute_mod_diff`` and ``compute_kubejs_diff`` log
their branch and result counts at DEBUG. ``render_changelog_html`` is
a pure string transform and logs only the report field counts at
DEBUG. ``write_changelog`` logs its path at DEBUG and success at INFO
(the second INFO site in the module, reached only on the direct-write
path; ``scope_client`` routes through ``render_changelog_html`` +
``atomic_write`` and does not call it). Pure helpers
(``_hash_bytes``, ``_esc``) and the ``DiffReport`` data class emit
nothing; the aggregate logs already cover their work.
"""

from __future__ import annotations

import hashlib
import html
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minecraft.deploy_pack.logging_setup import get_logger

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class DiffReport:
    """What changed in the client pack since the previous build.

    Pure data class; emits no events. The aggregate counts are logged
    by :func:`build_client_diff_report`.
    """

    added_mods: list[str] = field(default_factory=list)
    removed_mods: list[str] = field(default_factory=list)
    added_kubejs: list[str] = field(default_factory=list)
    modified_kubejs: list[str] = field(default_factory=list)
    removed_kubejs: list[str] = field(default_factory=list)
    initial_build: bool = False
    total_mods: int = 0

    def is_empty(self) -> bool:
        """True when there is nothing to report. Initial builds are never empty."""
        if self.initial_build:
            return False
        return not (self.added_mods or self.removed_mods or self.added_kubejs or self.modified_kubejs or self.removed_kubejs)

    def summary_line(self) -> str:
        """One-line teaser suitable for a Discord message body."""
        if self.initial_build:
            n = self.total_mods
            plural = "s" if n != 1 else ""
            return f"Initial build * {n} mod{plural} in pack"
        parts: list[str] = []
        if self.added_mods:
            n = len(self.added_mods)
            parts.append(f"{n} mod{('s' if n != 1 else '')} added")
        if self.removed_mods:
            n = len(self.removed_mods)
            parts.append(f"{n} mod{('s' if n != 1 else '')} removed")
        if self.added_kubejs:
            n = len(self.added_kubejs)
            parts.append(f"{n} KubeJS file{('s' if n != 1 else '')} added")
        if self.modified_kubejs:
            n = len(self.modified_kubejs)
            parts.append(f"{n} KubeJS file{('s' if n != 1 else '')} modified")
        if self.removed_kubejs:
            n = len(self.removed_kubejs)
            parts.append(f"{n} KubeJS file{('s' if n != 1 else '')} removed")
        if not parts:
            return "No changes since last build."
        return " * ".join(parts)


# ---------------------------------------------------------------------------
# Hashing and ZIP reads
# ---------------------------------------------------------------------------


def _hash_bytes(b: bytes) -> str:
    """SHA-256 of a byte string. Pure; no logging."""
    return hashlib.sha256(b).hexdigest()


def _zip_names(zip_path: Path, prefix: str, logger: Any = None) -> set[str]:
    """Relative paths of files under prefix in the zip. Directories excluded.

    Raises zipfile.BadZipFile or OSError on an unreadable ZIP; the
    caller (``scope_client``) wraps the raise at ERROR, so this helper
    does not catch.
    """
    if logger is None:
        logger = _log
    logger.debug(f"_zip_names: {zip_path} prefix={prefix!r}")
    result: set[str] = set()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix) :]
            if not rel or rel.endswith("/"):
                continue
            result.add(rel)
    logger.debug(f"_zip_names: {zip_path} prefix={prefix!r} -> {len(result)} file(s)")
    return result


def _zip_hashes(zip_path: Path, prefix: str, logger: Any = None) -> dict[str, str]:
    """{relative_path: sha256} for files under prefix in the zip.

    Raises zipfile.BadZipFile or OSError on an unreadable ZIP; the
    caller wraps the raise at ERROR, so this helper does not catch.
    """
    if logger is None:
        logger = _log
    logger.debug(f"_zip_hashes: {zip_path} prefix={prefix!r}")
    result: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix) :]
            if not rel or rel.endswith("/"):
                continue
            result[rel] = _hash_bytes(zf.read(name))
    logger.debug(f"_zip_hashes: {zip_path} prefix={prefix!r} -> {len(result)} hashed file(s)")
    return result


def _staging_hashes(root: Path, logger: Any = None) -> dict[str, str]:
    """{relative_path: sha256} for every file under root."""
    if logger is None:
        logger = _log
    if not root.is_dir():
        logger.debug(f"_staging_hashes: {root} is not a directory; returning empty")
        return {}
    result: dict[str, str] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        result[rel] = _hash_bytes(p.read_bytes())
    logger.debug(f"_staging_hashes: {root} -> {len(result)} file(s) hashed")
    return result


# ---------------------------------------------------------------------------
# Diffs
# ---------------------------------------------------------------------------


def compute_mod_diff(
    current_mods: set[str],
    previous_zip: Path | None,
    logger: Any = None,
) -> tuple[list[str], list[str]]:
    """Return (added, removed) mod filenames.

    Comparison is by filename only. A version bump shows up as one
    addition and one removal, which is what changed in the pack for
    the user.
    """
    if logger is None:
        logger = _log
    if previous_zip is None or not previous_zip.is_file():
        logger.debug(f"compute_mod_diff: no readable baseline ({previous_zip}); treating all {len(current_mods)} mod(s) as added (initial build)")
        return (sorted(current_mods), [])
    previous_mods = _zip_names(previous_zip, "mods/", logger)
    added = sorted(current_mods - previous_mods)
    removed = sorted(previous_mods - current_mods)
    logger.debug(f"compute_mod_diff: current={len(current_mods)} previous={len(previous_mods)} -> +{len(added)} -{len(removed)}")
    return (added, removed)


def compute_kubejs_diff(
    staging_kubejs: Path,
    previous_zip: Path | None,
    logger: Any = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return (added, modified, removed) KubeJS paths.

    Paths are relative to the kubejs directory and use forward slashes.
    Comparison is by content hash, so whitespace-only changes register
    as modifications.
    """
    if logger is None:
        logger = _log
    current = _staging_hashes(staging_kubejs, logger)
    if previous_zip is None or not previous_zip.is_file():
        logger.debug(f"compute_kubejs_diff: no readable baseline ({previous_zip}); treating all {len(current)} KubeJS file(s) as added (initial build)")
        return (sorted(current.keys()), [], [])
    previous = _zip_hashes(previous_zip, "kubejs/", logger)
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    modified = sorted(p for p in set(current) & set(previous) if current[p] != previous[p])
    logger.debug(f"compute_kubejs_diff: current={len(current)} previous={len(previous)} -> +{len(added)} ~{len(modified)} -{len(removed)}")
    return (added, modified, removed)


def build_client_diff_report(
    staging_dir: Path,
    previous_zip: Path | None,
    logger: Any = None,
) -> DiffReport:
    """Build a DiffReport from the current client staging vs previous ZIP.

    ``staging_dir`` is the directory that will become the new client
    ZIP. ``previous_zip`` is the most recent prior client ZIP, or None
    on a first build. In the None case the report is marked as an
    initial build and reports the total mod count instead of a diff.

    Emits exactly one INFO line per run: the ``summary_line``. The
    entry context and the per-stage counts land at DEBUG.
    """
    if logger is None:
        logger = _log

    logger.debug(f"build_client_diff_report: staging_dir={staging_dir} previous_zip={previous_zip}")
    if previous_zip is not None and not previous_zip.is_file():
        logger.warning(
            f"build_client_diff_report: previous_zip {previous_zip} was supplied but is not a file; "
            "treating this as an initial build (the changelog will report all current contents as new)"
        )

    staging_mods_dir = staging_dir / "mods"
    if staging_mods_dir.is_dir():
        current_mods = {p.name for p in staging_mods_dir.glob("*.jar")}
        logger.debug(f"build_client_diff_report: staging mods at {staging_mods_dir}: {len(current_mods)} .jar file(s)")
    else:
        current_mods = set()
        logger.warning(f"build_client_diff_report: staging mods directory missing: {staging_mods_dir}")

    staging_kubejs = staging_dir / "kubejs"

    added_mods, removed_mods = compute_mod_diff(current_mods, previous_zip, logger)
    added_kjs, modified_kjs, removed_kjs = compute_kubejs_diff(staging_kubejs, previous_zip, logger)

    is_initial = previous_zip is None or not previous_zip.is_file()
    report = DiffReport(
        added_mods=added_mods,
        removed_mods=removed_mods,
        added_kubejs=added_kjs,
        modified_kubejs=modified_kjs,
        removed_kubejs=removed_kjs,
        initial_build=is_initial,
        total_mods=len(current_mods),
    )
    logger.info(f"Client pack diff: {report.summary_line()}")
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _esc(text: str) -> str:
    """HTML-escape a string. Pure; no logging."""
    return html.escape(text, quote=True)


def render_changelog_html(
    report: DiffReport,
    artifact_name: str,
    artifact_url: str,
    sha256sum: str,
    timestamp: str,
    logger: Any = None,
) -> str:
    """Render the changelog page as a self-contained HTML document.

    Pure string transform; logs its inputs and the rendered length at
    DEBUG, no WARN/ERROR.
    """
    if logger is None:
        logger = _log
    logger.debug(
        f"render_changelog_html: artifact={artifact_name!r} initial_build={report.initial_build} "
        f"mods +{len(report.added_mods)} -{len(report.removed_mods)} "
        f"kubejs +{len(report.added_kubejs)} ~{len(report.modified_kubejs)} -{len(report.removed_kubejs)}"
    )
    lines: list[str] = []
    lines.append("<!DOCTYPE html>")
    lines.append('<html lang="en"><head>')
    lines.append('<meta charset="utf-8">')
    lines.append(f"<title>Client Pack - {_esc(artifact_name)}</title>")
    lines.append(
        "<style>body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2em auto; padding: 0 1em; color: #222; }code { background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-size: 0.95em; }.changelog { max-height: 500px; overflow-y: auto; border: 1px solid #ccc; padding: 1em; background: #fafafa; border-radius: 4px; }.changelog h3 { margin-top: 1.2em; }.changelog h3:first-child { margin-top: 0; }.empty { color: #888; font-style: italic; }h2 { margin-top: 1.6em; }</style>"
    )
    lines.append("</head><body>")
    lines.append(f"<h1>Client Pack - {_esc(artifact_name)}</h1>")
    lines.append(f"<p>Built {_esc(timestamp)} UTC</p>")
    lines.append("<h2>Download</h2>")
    lines.append(f'<p><a href="{_esc(artifact_url)}">{_esc(artifact_name)}</a></p>')
    lines.append(f"<p>SHA-256: <code>{_esc(sha256sum)}</code></p>")
    lines.append("<h2>Changelog</h2>")
    lines.append('<div class="changelog">')

    if report.initial_build:
        n = report.total_mods
        plural = "s" if n != 1 else ""
        lines.append(f'<p class="empty">Initial build - no previous pack to compare against. This pack contains {n} mod{plural}.</p>')
    elif report.is_empty():
        lines.append('<p class="empty">No changes since last build.</p>')
    else:
        if report.added_mods:
            lines.append("<h3>Added mods</h3><ol>")
            for mod in report.added_mods:
                lines.append(f"<li>{_esc(mod)}</li>")
            lines.append("</ol>")
        if report.removed_mods:
            lines.append("<h3>Removed mods</h3><ul>")
            for mod in report.removed_mods:
                lines.append(f"<li>{_esc(mod)}</li>")
            lines.append("</ul>")
        if report.added_kubejs:
            lines.append("<h3>Added KubeJS files</h3><ul>")
            for path in report.added_kubejs:
                lines.append(f"<li><code>{_esc(path)}</code></li>")
            lines.append("</ul>")
        if report.modified_kubejs:
            lines.append("<h3>Modified KubeJS files</h3><ul>")
            for path in report.modified_kubejs:
                lines.append(f"<li><code>{_esc(path)}</code></li>")
            lines.append("</ul>")
        if report.removed_kubejs:
            lines.append("<h3>Removed KubeJS files</h3><ul>")
            for path in report.removed_kubejs:
                lines.append(f"<li><code>{_esc(path)}</code></li>")
            lines.append("</ul>")

    lines.append("</div>")
    lines.append("</body></html>")
    html_text = "\n".join(lines)
    logger.debug(f"render_changelog_html: rendered {len(html_text)} char(s)")
    return html_text


def write_changelog(
    report: DiffReport,
    artifact_name: str,
    artifact_url: str,
    sha256sum: str,
    timestamp: str,
    output_path: Path,
    logger: Any = None,
) -> None:
    """Write the changelog HTML page to output_path.

    OSError from ``mkdir`` or ``write_text`` propagates to the caller;
    the scope layer wraps it at ERROR, so this function does not catch.
    """
    if logger is None:
        logger = _log
    logger.debug(f"write_changelog: writing to {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_changelog_html(report, artifact_name, artifact_url, sha256sum, timestamp, logger)
    output_path.write_text(html_text, encoding="utf-8")
    logger.info(f"write_changelog: wrote {len(html_text)} char(s) to {output_path}")
