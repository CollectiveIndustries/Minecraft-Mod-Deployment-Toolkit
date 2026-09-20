# src/minecraft/common/changelog.py

"""Changelog generation: diff the current client pack against the
previous one and render human-readable HTML.

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
"""  # noqa: D205

from __future__ import annotations

import hashlib
import html
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class DiffReport:
    """What changed in the client pack since the previous build."""

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
    return hashlib.sha256(b).hexdigest()


def _zip_names(zip_path: Path, prefix: str) -> set[str]:
    """Relative paths of files under prefix in the zip. Directories excluded."""
    result: set[str] = set()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix) :]
            if not rel or rel.endswith("/"):
                continue
            result.add(rel)
    return result


def _zip_hashes(zip_path: Path, prefix: str) -> dict[str, str]:
    """{relative_path: sha256} for files under prefix in the zip."""
    result: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix) :]
            if not rel or rel.endswith("/"):
                continue
            result[rel] = _hash_bytes(zf.read(name))
    return result


def _staging_hashes(root: Path) -> dict[str, str]:
    """{relative_path: sha256} for every file under root."""
    if not root.is_dir():
        return {}
    result: dict[str, str] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        result[rel] = _hash_bytes(p.read_bytes())
    return result


# ---------------------------------------------------------------------------
# Diffs
# ---------------------------------------------------------------------------


def compute_mod_diff(
    current_mods: set[str],
    previous_zip: Path | None,
) -> tuple[list[str], list[str]]:
    """Return (added, removed) mod filenames.

    Comparison is by filename only. A version bump shows up as one
    addition and one removal, which is what changed in the pack for
    the user.
    """
    if previous_zip is None or not previous_zip.is_file():
        return (sorted(current_mods), [])
    previous_mods = _zip_names(previous_zip, "mods/")
    added = sorted(current_mods - previous_mods)
    removed = sorted(previous_mods - current_mods)
    return (added, removed)


def compute_kubejs_diff(
    staging_kubejs: Path,
    previous_zip: Path | None,
) -> tuple[list[str], list[str], list[str]]:
    """Return (added, modified, removed) KubeJS paths.

    Paths are relative to the kubejs directory and use forward slashes.
    Comparison is by content hash, so whitespace-only changes register
    as modifications.
    """
    current = _staging_hashes(staging_kubejs)
    if previous_zip is None or not previous_zip.is_file():
        return (sorted(current.keys()), [], [])
    previous = _zip_hashes(previous_zip, "kubejs/")
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    modified = sorted(p for p in set(current) & set(previous) if current[p] != previous[p])
    return (added, modified, removed)


def build_client_diff_report(
    staging_dir: Path,
    previous_zip: Path | None,
    logger,
) -> DiffReport:
    """Build a DiffReport from the current client staging vs previous ZIP.

    ``staging_dir`` is the directory that will become the new client
    ZIP. ``previous_zip`` is the most recent prior client ZIP, or None
    on a first build. In the None case the report is marked as an
    initial build and reports the total mod count instead of a diff.
    """
    staging_mods_dir = staging_dir / "mods"
    if staging_mods_dir.is_dir():
        current_mods = {p.name for p in staging_mods_dir.glob("*.jar")}
    else:
        current_mods = set()

    staging_kubejs = staging_dir / "kubejs"

    added_mods, removed_mods = compute_mod_diff(current_mods, previous_zip)
    added_kjs, modified_kjs, removed_kjs = compute_kubejs_diff(staging_kubejs, previous_zip)

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
    return html.escape(text, quote=True)


def render_changelog_html(
    report: DiffReport,
    artifact_name: str,
    artifact_url: str,
    sha256sum: str,
    timestamp: str,
) -> str:
    """Render the changelog page as a self-contained HTML document."""
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
    return "\n".join(lines)


def write_changelog(
    report: DiffReport,
    artifact_name: str,
    artifact_url: str,
    sha256sum: str,
    timestamp: str,
    output_path: Path,
) -> None:
    """Write the changelog HTML page to output_path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_changelog_html(report, artifact_name, artifact_url, sha256sum, timestamp)
    output_path.write_text(html_text, encoding="utf-8")
