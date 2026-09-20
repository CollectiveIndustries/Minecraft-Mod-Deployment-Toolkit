# src/minecraft/common/changelog.py

"""Changelog generation: diff source vs target, render human-readable HTML.

Read-only against both source and target. The only file this module
writes is the final HTML page, which lands in www_dir alongside the
client ZIP.

"Human-readable" here means a structured list of what changed, not a
unified diff dump. The page lists:

  - added mods (by jar filename)
  - added KubeJS files (by path)
  - modified KubeJS files (by path)
  - removed KubeJS files (by path)

No network calls, no LLM, no git. The output is deterministic and can
be diffed across builds.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DiffReport:
    """What changed between source and target at the moment of build."""

    added_mods: list[str] = field(default_factory=list)
    added_kubejs: list[str] = field(default_factory=list)
    modified_kubejs: list[str] = field(default_factory=list)
    removed_kubejs: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """Checks whether there are no tracked modifications."""
        return not (self.added_mods or self.added_kubejs or self.modified_kubejs or self.removed_kubejs)

    def summary_line(self) -> str:
        """One-line teaser suitable for a Discord message body."""
        parts: list[str] = []
        if self.added_mods:
            n = len(self.added_mods)
            parts.append(f"{n} mod{('s' if n != 1 else '')} added")
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


def _relative_files(root: Path) -> set[Path]:
    """Return all files under root as paths relative to root."""
    if not root.is_dir():
        return set()
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


def compute_mod_diff(wanted_files: list[str], target_mods_dir: Path) -> list[str]:
    """Return mod filenames that are wanted but not yet in target_mods_dir.

    ``wanted_files`` is the list of jar filenames for the side being
    deployed (already filtered from the Prism index). The target is the
    shared mods directory. Order is alphabetical for stable output.
    """
    current: set[str] = set()
    if target_mods_dir.is_dir():
        current = {p.name for p in target_mods_dir.glob("*.jar")}
    return sorted(name for name in wanted_files if name not in current)


def compute_kubejs_diff(source_kubejs: Path, target_kubejs: Path) -> tuple[list[str], list[str], list[str]]:
    """Return (added, modified, removed) KubeJS paths.

    Paths are relative to the kubejs directory and use forward slashes
    for stability across platforms.
    """
    src = _relative_files(source_kubejs)
    tgt = _relative_files(target_kubejs)
    added = sorted(str(p).replace("\\", "/") for p in src - tgt)
    removed = sorted(str(p).replace("\\", "/") for p in tgt - src)
    modified: list[str] = []
    for rel in sorted(src & tgt):
        try:
            if (source_kubejs / rel).read_bytes() != (target_kubejs / rel).read_bytes():
                modified.append(str(rel).replace("\\", "/"))
        except OSError:
            continue
    return (added, modified, removed)


def build_diff_report(wanted_mod_files: list[str], target_mods_dir: Path, source_kubejs: Path, target_kubejs: Path) -> DiffReport:
    """Build a DiffReport from the source/target inputs."""
    added, modified, removed = compute_kubejs_diff(source_kubejs, target_kubejs)
    return DiffReport(added_mods=compute_mod_diff(wanted_mod_files, target_mods_dir), added_kubejs=added, modified_kubejs=modified, removed_kubejs=removed)


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def render_changelog_html(report: DiffReport, artifact_name: str, artifact_url: str, sha256sum: str, timestamp: str) -> str:
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
    if report.is_empty():
        lines.append('<p class="empty">No changes since last build.</p>')
    else:
        if report.added_mods:
            lines.append("<h3>Added mods</h3><ol>")
            for mod in report.added_mods:
                lines.append(f"<li>{_esc(mod)}</li>")
            lines.append("</ol>")
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


def write_changelog(report: DiffReport, artifact_name: str, artifact_url: str, sha256sum: str, timestamp: str, output_path: Path) -> None:
    """Write the changelog HTML page to output_path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_changelog_html(report, artifact_name, artifact_url, sha256sum, timestamp)
    output_path.write_text(html_text, encoding="utf-8")
