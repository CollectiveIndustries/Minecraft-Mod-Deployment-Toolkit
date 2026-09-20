# tests/unit/common/test_changelog.py

"""Unit tests for changelog.py.

The changelog describes the client pack by diffing the current staging
tree against the most recent previous client ZIP. These tests exercise
the ZIP-reading comparison, the initial-build case, and the renderer.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from src.minecraft.common import changelog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, content: str = "x") -> None:
    """Write content to root/rel, creating parent directories as needed."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _zip_with(
    path: Path,
    mods: list[str],
    kubejs: dict[str, str] | None = None,
) -> Path:
    """Write a minimal client ZIP with the given mods/ and kubejs/ contents.

    Used as a stand-in for the previous build's client pack.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for m in mods:
            zf.writestr(f"mods/{m}", b"")
        for rel, content in (kubejs or {}).items():
            zf.writestr(f"kubejs/{rel}", content)
    return path


# ---------------------------------------------------------------------------
# DiffReport
# ---------------------------------------------------------------------------


class TestDiffReport:
    """Tests for the DiffReport dataclass."""

    def test_is_empty_true_for_fresh(self):
        """A freshly constructed DiffReport has no sections populated."""
        assert changelog.DiffReport().is_empty()

    def test_is_empty_false_when_any_section_populated(self):
        """is_empty returns False when any of the sections has entries."""
        assert not changelog.DiffReport(added_mods=["a.jar"]).is_empty()
        assert not changelog.DiffReport(removed_mods=["a.jar"]).is_empty()
        assert not changelog.DiffReport(added_kubejs=["x"]).is_empty()
        assert not changelog.DiffReport(modified_kubejs=["x"]).is_empty()
        assert not changelog.DiffReport(removed_kubejs=["x"]).is_empty()

    def test_is_empty_false_for_initial_build(self):
        """An initial build is never empty, even with no diff entries."""
        assert not changelog.DiffReport(initial_build=True, total_mods=0).is_empty()

    def test_summary_line_empty(self):
        """An empty non-initial report summarises to a 'no changes' message."""
        assert "no changes" in changelog.DiffReport().summary_line().lower()

    def test_summary_line_initial(self):
        """An initial build reports the total mod count."""
        r = changelog.DiffReport(initial_build=True, total_mods=101)
        assert "Initial build" in r.summary_line()
        assert "101 mods" in r.summary_line()

    def test_summary_line_initial_singular(self):
        """An initial build with one mod uses the singular noun."""
        r = changelog.DiffReport(initial_build=True, total_mods=1)
        assert "1 mod in pack" in r.summary_line()

    def test_summary_line_plural(self):
        """Counts greater than one use plural nouns."""
        r = changelog.DiffReport(
            added_mods=["a", "b"],
            modified_kubejs=["x", "y", "z"],
        )
        s = r.summary_line()
        assert "2 mods added" in s
        assert "3 KubeJS files modified" in s

    def test_summary_line_singular(self):
        """A count of one uses the singular noun."""
        r = changelog.DiffReport(added_mods=["a"])
        assert "1 mod added" in r.summary_line()

    def test_summary_line_reports_removals(self):
        """Removals appear in the summary."""
        r = changelog.DiffReport(removed_mods=["a", "b"])
        assert "2 mods removed" in r.summary_line()


# ---------------------------------------------------------------------------
# compute_mod_diff
# ---------------------------------------------------------------------------


class TestComputeModDiff:
    """Tests for compute_mod_diff against a previous ZIP."""

    def test_added_mods(self, tmp_path):
        """Only filenames not in the previous ZIP are returned as added."""
        prev = _zip_with(tmp_path / "prev.zip", ["existing.jar"])
        added, removed = changelog.compute_mod_diff({"existing.jar", "new1.jar", "new2.jar"}, prev)
        assert added == ["new1.jar", "new2.jar"]
        assert removed == []

    def test_removed_mods(self, tmp_path):
        """Filenames in the previous but not current are returned as removed."""
        prev = _zip_with(tmp_path / "prev.zip", ["kept.jar", "gone.jar"])
        added, removed = changelog.compute_mod_diff({"kept.jar"}, prev)
        assert added == []
        assert removed == ["gone.jar"]

    def test_added_and_removed_together(self, tmp_path):
        """Both sides of the diff are reported independently."""
        prev = _zip_with(tmp_path / "prev.zip", ["old.jar"])
        added, removed = changelog.compute_mod_diff({"new.jar"}, prev)
        assert added == ["new.jar"]
        assert removed == ["old.jar"]

    def test_identical_sets_produce_empty_diff(self, tmp_path):
        """No change means no diff entries."""
        prev = _zip_with(tmp_path / "prev.zip", ["a.jar", "b.jar"])
        added, removed = changelog.compute_mod_diff({"a.jar", "b.jar"}, prev)
        assert added == []
        assert removed == []

    def test_no_previous_zip_reports_all_as_added(self, tmp_path):
        """With no previous ZIP every current mod is reported as added."""
        added, removed = changelog.compute_mod_diff({"a.jar", "b.jar"}, None)
        assert added == ["a.jar", "b.jar"]
        assert removed == []

    def test_nonexistent_previous_zip_reports_all_as_added(self, tmp_path):
        """A path that isn't a file behaves the same as None."""
        added, removed = changelog.compute_mod_diff({"a.jar"}, tmp_path / "missing.zip")
        assert added == ["a.jar"]
        assert removed == []

    def test_output_is_sorted(self, tmp_path):
        """Both lists are sorted for stable output across builds."""
        prev = _zip_with(tmp_path / "prev.zip", ["z.jar", "a.jar"])
        added, removed = changelog.compute_mod_diff({"m.jar", "b.jar"}, prev)
        assert added == ["b.jar", "m.jar"]
        assert removed == ["a.jar", "z.jar"]


# ---------------------------------------------------------------------------
# compute_kubejs_diff
# ---------------------------------------------------------------------------


class TestComputeKubejsDiff:
    """Tests for compute_kubejs_diff against a previous ZIP."""

    def test_added_modified_removed(self, tmp_path):
        """The diff splits KubeJS changes into three sorted lists."""
        src = tmp_path / "kubejs"
        _write(src, "a.js", "AAA")
        _write(src, "b.js", "BBB")
        _write(src, "sub/c.js", "CCC")

        prev = _zip_with(
            tmp_path / "prev.zip",
            [],
            kubejs={
                "b.js": "CHANGED",
                "old.js": "OLD",
                "sub/d.js": "DDD",
            },
        )

        added, modified, removed = changelog.compute_kubejs_diff(src, prev)
        assert added == ["a.js", "sub/c.js"]
        assert modified == ["b.js"]
        assert removed == ["old.js", "sub/d.js"]

    def test_identical_content_produces_empty_diff(self, tmp_path):
        """Same content on both sides means no diff entries."""
        src = tmp_path / "kubejs"
        _write(src, "a.js", "AAA")
        prev = _zip_with(tmp_path / "prev.zip", [], kubejs={"a.js": "AAA"})
        added, modified, removed = changelog.compute_kubejs_diff(src, prev)
        assert added == []
        assert modified == []
        assert removed == []

    def test_whitespace_change_registers_as_modified(self, tmp_path):
        """Comparison is by content hash, so whitespace matters."""
        src = tmp_path / "kubejs"
        _write(src, "a.js", "AAA\n")
        prev = _zip_with(tmp_path / "prev.zip", [], kubejs={"a.js": "AAA"})
        _, modified, _ = changelog.compute_kubejs_diff(src, prev)
        assert modified == ["a.js"]

    def test_no_previous_zip_reports_all_as_added(self, tmp_path):
        """With no previous ZIP every current KubeJS file is added."""
        src = tmp_path / "kubejs"
        _write(src, "a.js", "AAA")
        _write(src, "b.js", "BBB")
        added, modified, removed = changelog.compute_kubejs_diff(src, None)
        assert added == ["a.js", "b.js"]
        assert modified == []
        assert removed == []

    def test_missing_source_dir_treats_all_as_removed(self, tmp_path):
        """A missing source dir means everything previous is now gone."""
        prev = _zip_with(tmp_path / "prev.zip", [], kubejs={"a.js": "AAA"})
        added, modified, removed = changelog.compute_kubejs_diff(tmp_path / "nope", prev)
        assert added == []
        assert modified == []
        assert removed == ["a.js"]

    def test_both_missing_yields_empty(self, tmp_path):
        """Missing source and no previous ZIP yields empty lists."""
        added, modified, removed = changelog.compute_kubejs_diff(tmp_path / "nope", None)
        assert added == []
        assert modified == []
        assert removed == []


# ---------------------------------------------------------------------------
# build_client_diff_report
# ---------------------------------------------------------------------------


class TestBuildClientDiffReport:
    """Integration tests for the report builder."""

    def test_initial_build_flagged(self, tmp_path):
        """No previous ZIP means initial_build=True and total_mods is set."""
        staging = tmp_path / "staging"
        (staging / "mods").mkdir(parents=True)
        (staging / "mods" / "a.jar").write_bytes(b"")
        (staging / "mods" / "b.jar").write_bytes(b"")

        report = changelog.build_client_diff_report(
            staging_dir=staging,
            previous_zip=None,
            logger=MagicMock(),
        )
        assert report.initial_build is True
        assert report.total_mods == 2
        assert report.added_mods == ["a.jar", "b.jar"]

    def test_diff_against_previous_zip(self, tmp_path):
        """With a previous ZIP the report describes additions and removals."""
        staging = tmp_path / "staging"
        (staging / "mods").mkdir(parents=True)
        (staging / "mods" / "kept.jar").write_bytes(b"")
        (staging / "mods" / "new.jar").write_bytes(b"")
        (staging / "kubejs").mkdir()
        _write(staging / "kubejs", "changed.js", "NEW")
        _write(staging / "kubejs", "added.js", "AAA")

        prev = _zip_with(
            tmp_path / "prev.zip",
            ["kept.jar", "gone.jar"],
            kubejs={"changed.js": "OLD", "removed.js": "X"},
        )

        report = changelog.build_client_diff_report(
            staging_dir=staging,
            previous_zip=prev,
            logger=MagicMock(),
        )
        assert report.initial_build is False
        assert report.added_mods == ["new.jar"]
        assert report.removed_mods == ["gone.jar"]
        assert report.added_kubejs == ["added.js"]
        assert report.modified_kubejs == ["changed.js"]
        assert report.removed_kubejs == ["removed.js"]

    def test_empty_staging_no_previous(self, tmp_path):
        """An empty staging dir with no previous ZIP is still an initial build."""
        staging = tmp_path / "staging"
        staging.mkdir()
        report = changelog.build_client_diff_report(
            staging_dir=staging,
            previous_zip=None,
            logger=MagicMock(),
        )
        assert report.initial_build is True
        assert report.total_mods == 0
        assert report.added_mods == []

    def test_logs_summary_line(self, tmp_path):
        """The builder logs a summary line for operators reading the log."""
        staging = tmp_path / "staging"
        (staging / "mods").mkdir(parents=True)
        (staging / "mods" / "a.jar").write_bytes(b"")
        logger = MagicMock()
        changelog.build_client_diff_report(
            staging_dir=staging,
            previous_zip=None,
            logger=logger,
        )
        logger.info.assert_called_once()
        assert "Initial build" in logger.info.call_args[0][0]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRenderHtml:
    """Tests for render_changelog_html."""

    def test_empty_report_says_no_changes(self):
        """An empty report renders the placeholder text and download info."""
        html = changelog.render_changelog_html(
            report=changelog.DiffReport(),
            artifact_name="pack.zip",
            artifact_url="https://x.test/pack.zip",
            sha256sum="deadbeef",
            timestamp="2026-09-20 12:00:00",
        )
        assert "No changes since last build." in html
        assert "deadbeef" in html
        assert "pack.zip" in html

    def test_initial_build_reports_total(self):
        """An initial build reports the total mod count instead of a diff."""
        html = changelog.render_changelog_html(
            report=changelog.DiffReport(initial_build=True, total_mods=101),
            artifact_name="pack.zip",
            artifact_url="https://x.test/pack.zip",
            sha256sum="deadbeef",
            timestamp="2026-09-20 12:00:00",
        )
        assert "Initial build" in html
        assert "101 mods" in html

    def test_initial_build_singular(self):
        """An initial build with one mod uses the singular noun."""
        html = changelog.render_changelog_html(
            report=changelog.DiffReport(initial_build=True, total_mods=1),
            artifact_name="pack.zip",
            artifact_url="https://x.test/pack.zip",
            sha256sum="deadbeef",
            timestamp="2026-09-20 12:00:00",
        )
        assert "1 mod" in html
        assert "1 mods" not in html

    def test_populated_report_lists_all_sections(self):
        """Every populated section appears in the rendered HTML."""
        report = changelog.DiffReport(
            added_mods=["new.jar"],
            removed_mods=["old.jar"],
            added_kubejs=["a.js"],
            modified_kubejs=["b.js"],
            removed_kubejs=["gone.js"],
        )
        html = changelog.render_changelog_html(
            report=report,
            artifact_name="pack.zip",
            artifact_url="https://x.test/pack.zip",
            sha256sum="abcd",
            timestamp="2026-09-20 12:00:00",
        )
        for needle in (
            "Added mods",
            "new.jar",
            "Removed mods",
            "old.jar",
            "Added KubeJS files",
            "a.js",
            "Modified KubeJS files",
            "b.js",
            "Removed KubeJS files",
            "gone.js",
        ):
            assert needle in html

    def test_missing_sections_are_omitted(self):
        """Sections with no entries do not appear in the HTML."""
        report = changelog.DiffReport(added_mods=["only.jar"])
        html = changelog.render_changelog_html(
            report=report,
            artifact_name="pack.zip",
            artifact_url="https://x.test/pack.zip",
            sha256sum="abcd",
            timestamp="2026-09-20 12:00:00",
        )
        assert "Added mods" in html
        assert "Removed mods" not in html
        assert "KubeJS files" not in html

    def test_html_escapes_special_chars(self):
        """Angle brackets in filenames are escaped to prevent injection."""
        report = changelog.DiffReport(added_mods=["<script>.jar"])
        html = changelog.render_changelog_html(
            report=report,
            artifact_name="pack.zip",
            artifact_url="https://x.test/pack.zip",
            sha256sum="abcd",
            timestamp="2026-09-20 12:00:00",
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# write_changelog
# ---------------------------------------------------------------------------


def test_write_changelog_creates_file(tmp_path):
    """write_changelog writes the rendered HTML to the requested path."""
    out = tmp_path / "out" / "changelog.html"
    changelog.write_changelog(
        report=changelog.DiffReport(added_mods=["a.jar"]),
        artifact_name="pack.zip",
        artifact_url="https://x.test/pack.zip",
        sha256sum="ff",
        timestamp="2026-09-20 12:00:00",
        output_path=out,
    )
    assert out.is_file()
    assert "a.jar" in out.read_text(encoding="utf-8")
