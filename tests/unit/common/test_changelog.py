# tests/unit/common/test_changelog.py

"""Unit tests for changelog.py."""

from __future__ import annotations

from pathlib import Path

from src.minecraft.common import changelog


def _write(root: Path, rel: str, content: str = "x") -> None:
    """Write content to root/rel, creating parent directories as needed."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class TestDiffReport:
    """Tests for the DiffReport dataclass."""

    def test_is_empty_true_for_fresh(self):
        """A freshly constructed DiffReport has no sections populated."""
        assert changelog.DiffReport().is_empty()

    def test_is_empty_false_when_any_section_populated(self):
        """is_empty returns False when any of the four sections has entries."""
        assert not changelog.DiffReport(added_mods=["a.jar"]).is_empty()
        assert not changelog.DiffReport(added_kubejs=["x"]).is_empty()
        assert not changelog.DiffReport(modified_kubejs=["x"]).is_empty()
        assert not changelog.DiffReport(removed_kubejs=["x"]).is_empty()

    def test_summary_line_empty(self):
        """An empty report summarises to a 'no changes' message."""
        assert "no changes" in changelog.DiffReport().summary_line().lower()

    def test_summary_line_plural(self):
        """Counts greater than one use plural nouns."""
        r = changelog.DiffReport(added_mods=["a", "b"], modified_kubejs=["x", "y", "z"])
        s = r.summary_line()
        assert "2 mods added" in s
        assert "3 KubeJS files modified" in s

    def test_summary_line_singular(self):
        """A count of one uses the singular noun."""
        r = changelog.DiffReport(added_mods=["a"])
        assert "1 mod added" in r.summary_line()


class TestComputeModDiff:
    """Tests for compute_mod_diff."""

    def test_added_mods(self, tmp_path):
        """Only mod filenames missing from the target directory are returned."""
        target = tmp_path / "mods"
        target.mkdir()
        (target / "existing.jar").write_text("x")
        wanted = ["existing.jar", "new1.jar", "new2.jar"]
        assert changelog.compute_mod_diff(wanted, target) == ["new1.jar", "new2.jar"]

    def test_missing_target_dir(self, tmp_path):
        """A missing target directory means every wanted mod is new."""
        wanted = ["a.jar", "b.jar"]
        assert changelog.compute_mod_diff(wanted, tmp_path / "nope") == ["a.jar", "b.jar"]


class TestComputeKubejsDiff:
    """Tests for compute_kubejs_diff."""

    def test_added_modified_removed(self, tmp_path):
        """The diff splits changes into three sorted lists."""
        src = tmp_path / "src_kjs"
        tgt = tmp_path / "tgt_kjs"
        _write(src, "a.js", "AAA")
        _write(src, "b.js", "BBB")
        _write(src, "sub/c.js", "CCC")

        _write(tgt, "b.js", "CHANGED")
        _write(tgt, "old.js", "OLD")
        _write(tgt, "sub/d.js", "DDD")

        added, modified, removed = changelog.compute_kubejs_diff(src, tgt)
        assert added == ["a.js", "sub/c.js"]
        assert modified == ["b.js"]
        assert removed == ["old.js", "sub/d.js"]

    def test_missing_dirs(self, tmp_path):
        """Missing source and target directories yield empty lists."""
        added, modified, removed = changelog.compute_kubejs_diff(tmp_path / "nope_src", tmp_path / "nope_tgt")
        assert added == []
        assert modified == []
        assert removed == []


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

    def test_populated_report_lists_sections(self):
        """Every populated section appears in the rendered HTML."""
        report = changelog.DiffReport(
            added_mods=["new.jar"],
            added_kubejs=["a.js"],
            modified_kubejs=["b.js"],
            removed_kubejs=["old.js"],
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
            "Added KubeJS files",
            "a.js",
            "Modified KubeJS files",
            "b.js",
            "Removed KubeJS files",
            "old.js",
        ):
            assert needle in html

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
