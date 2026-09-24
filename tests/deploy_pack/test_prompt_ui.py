# tests/deploy_pack/test_prompt_ui.py

"""Tests for deploy_pack.prompt_ui.

The pure model is tested unconditionally. Textual-specific tests are
skipped when Textual isn't installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.deploy_pack.config_model import DeploymentConfig, DiscordConfig, DockerConfig
from minecraft.deploy_pack.overrides import SideOverrides
from minecraft.deploy_pack.prompt_ui import HAS_TEXTUAL, AuditRow, build_audit_rows, compute_review_entries


def _config(tmp_path: Path, *, index_entries: dict[str, dict] | None = None, overrides_toml: str | None = None) -> DeploymentConfig:
    downloads = tmp_path / "sync" / "downloads"
    index_dir = downloads / ".index"
    index_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = tmp_path / "config.d"
    cfg_dir.mkdir(exist_ok=True)
    if index_entries:
        for stem, body in index_entries.items():
            lines = [f'''filename = "{body["filename"]}"''']
            if "side" in body:
                lines.append(f'''side = "{body["side"]}"''')
            if "name" in body:
                lines.append(f'''name = "{body["name"]}"''')
            (index_dir / f"{stem}.pw.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if overrides_toml is not None:
        (cfg_dir / "side_overrides.toml").write_text(overrides_toml, encoding="utf-8")
    return DeploymentConfig(
        project_root=tmp_path,
        config_dir=cfg_dir,
        sync_root=tmp_path / "sync",
        modpack_dir=downloads,
        www_dir=None,
        www_dir_error=None,
        www_dir_candidates=[],
        output_filename="pack.zip",
        download_base_url="http://x",
        protect_file=None,
        sync_mapping={},
        restart_policy={},
        instances={},
        partition=[],
        partition_unknown=[],
        requested_instances=None,
        resource_packs={},
        docker=DockerConfig(compose_file=tmp_path / "dc.yml"),
        discord=DiscordConfig(),
        webhook_url=None,
        compose=None,
        mods_dir_toml=None,
    )


def _pw(tmp_path: Path, filename: str, *, side: str | None = "both", stem: str | None = None) -> None:
    index_dir = tmp_path / "sync" / "downloads" / ".index"
    index_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or filename.replace(".jar", "")
    lines = [f'filename = "{filename}"']
    if side is not None:
        lines.append(f'side = "{side}"')
    (index_dir / f"{stem}.pw.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _row() -> AuditRow:
    return AuditRow(filename="x.jar", declared_side="both", declared_source="x.pw.toml", override_value=None, override_section=None)


def test_toggle_s_sets_server() -> None:
    """Tests that toggling 's' sets the server review value."""
    r = _row()
    r.toggle("s")
    assert r.toggle_s
    assert not r.toggle_c
    assert r.review_value == "server"


def test_toggle_c_sets_client() -> None:
    """Tests that toggling 'c' sets the client review value."""
    r = _row()
    r.toggle("c")
    assert r.review_value == "client"


def test_toggle_s_and_c_sets_both() -> None:
    """Tests that toggling both 's' and 'c' sets the review value to both."""
    r = _row()
    r.toggle("s")
    r.toggle("c")
    assert r.review_value == "both"


def test_toggle_s_off_leaves_c() -> None:
    """Verify toggling 's' off after 'c' is set preserves the 'client' review value."""
    r = _row()
    r.toggle("s")
    r.toggle("c")
    r.toggle("s")
    assert r.review_value == "client"


def test_toggle_n_sets_skipped() -> None:
    """Verify toggling 'n' sets the review value to 'skipped'."""
    r = _row()
    r.toggle("n")
    assert r.review_value == "skipped"


def test_toggle_n_exclusive_with_s_c() -> None:
    """Verify toggling 'n' after 's' clears 's' and 'c' flags, leaving only 'n' active."""
    r = _row()
    r.toggle("s")
    r.toggle("n")
    assert r.toggle_n
    assert not r.toggle_s
    assert not r.toggle_c


def test_toggle_s_clears_n() -> None:
    """Verify toggling 's' after 'n' clears the 'n' flag."""
    r = _row()
    r.toggle("n")
    r.toggle("s")
    assert r.toggle_s
    assert not r.toggle_n


def test_toggle_d_sets_removal() -> None:
    """Verify toggling 'd' sets the removal flag and clears the review value."""
    r = _row()
    r.toggle("d")
    assert r.toggle_d
    assert r.review_value is None


def test_toggle_d_exclusive_with_others() -> None:
    """Tests that toggling "d" turns off the "s" and "c" toggles."""
    r = _row()
    r.toggle("s")
    r.toggle("c")
    r.toggle("d")
    assert r.toggle_d
    assert not r.toggle_s
    assert not r.toggle_c


def test_toggle_all_off_is_no_entry() -> None:
    """Tests that a row with all toggles off has no review entry."""
    r = _row()
    assert r.review_value is None


def test_toggle_d_off_after_on() -> None:
    """Tests that toggling "d" twice turns it off and resets the review value to None."""
    r = _row()
    r.toggle("d")
    r.toggle("d")
    assert not r.toggle_d
    assert r.review_value is None


def test_toggle_unknown_key_raises() -> None:
    """Tests that toggling an unknown key raises a ValueError."""
    with pytest.raises(ValueError):
        _row().toggle("x")


def test_format_declared_with_source() -> None:
    """Tests that format_declared returns the declared side annotated with its source file."""
    r = _row()
    assert r.format_declared() == "both (from x.pw.toml)"


def test_format_declared_without_source() -> None:
    """Tests formatting declared source when no source is provided."""
    r = _row()
    r.declared_source = ""
    assert r.format_declared() == "both"


def test_format_override_none() -> None:
    """Tests formatting an override when no override is set."""
    assert _row().format_override() == "-"


def test_format_override_present() -> None:
    """Tests formatting an override when both value and section are present."""
    r = _row()
    r.override_value = "client"
    r.override_section = "by_filename"
    assert r.format_override() == "client (by_filename)"


def test_format_toggle() -> None:
    """Tests formatting a toggle state before and after toggling."""
    r = _row()
    assert r.format_toggle("s") == "[ ]"
    r.toggle_s = True
    assert r.format_toggle("s") == "[X]"


def test_from_entry_no_override() -> None:
    """Tests creating an AuditRow from an entry without overrides."""
    entry = {"file": "x.jar", "side_raw": "server", "side": "server", "index_file": "x.pw.toml", "id": "x"}
    row = AuditRow.from_entry(entry, SideOverrides())
    assert row is not None
    assert row.filename == "x.jar"
    assert row.declared_side == "server"
    assert row.declared_source == "x.pw.toml"
    assert row.override_value is None
    assert row.review_value is None


def test_from_entry_no_side_key_uses_both() -> None:
    """Tests that AuditRow.from_entry defaults the declared side to "both" when side_raw is None."""
    entry = {"file": "x.jar", "side_raw": None, "side": "both", "index_file": "x.pw.toml", "id": "x"}
    row = AuditRow.from_entry(entry, SideOverrides())
    assert row is not None
    assert row.declared_side == "both"


def test_from_entry_by_id_override() -> None:
    """Tests that AuditRow.from_entry populates override fields from a by_id override while leaving review-based toggles unset."""
    entry = {"file": "x.jar", "side_raw": "both", "side": "both", "index_file": "x.pw.toml", "id": "123"}
    overrides = SideOverrides(by_id={"123": "client"})
    row = AuditRow.from_entry(entry, overrides)
    assert row.override_value == "client"
    assert row.override_section == "by_id"
    assert row.review_value is None


def test_from_entry_review_init_server() -> None:
    """Tests that AuditRow.from_entry enables only the server toggle when the deployment tool review override is set to "server"."""
    entry = {"file": "x.jar", "side_raw": "both", "side": "both", "index_file": "x.pw.toml", "id": "x"}
    overrides = SideOverrides(deployment_tool_review={"x.jar": "server"})
    row = AuditRow.from_entry(entry, overrides)
    assert row.toggle_s
    assert row.review_value == "server"


def test_from_entry_review_init_both() -> None:
    """Tests that AuditRow.from_entry enables both side toggles when the deployment tool review override is set to "both"."""
    entry = {"file": "x.jar", "side_raw": "both", "side": "both", "index_file": "x.pw.toml", "id": "x"}
    overrides = SideOverrides(deployment_tool_review={"x.jar": "both"})
    row = AuditRow.from_entry(entry, overrides)
    assert row.toggle_s and row.toggle_c
    assert row.review_value == "both"


def test_from_entry_review_init_skipped() -> None:
    """Tests that an entry with a skipped deployment tool review sets the row toggle to false."""
    entry = {"file": "x.jar", "side_raw": "both", "side": "both", "index_file": "x.pw.toml", "id": "x"}
    overrides = SideOverrides(deployment_tool_review={"x.jar": "skipped"})
    row = AuditRow.from_entry(entry, overrides)
    assert row.toggle_n


def test_from_entry_missing_filename() -> None:
    """Tests that from_entry returns None when the entry lacks a filename."""
    assert AuditRow.from_entry({}, SideOverrides()) is None


def test_build_audit_rows_empty(tmp_path: Path) -> None:
    """Tests that build_audit_rows returns an empty list when no files are present."""
    cfg = _config(tmp_path)
    assert build_audit_rows(cfg) == []


def test_build_audit_rows_sorted(tmp_path: Path) -> None:
    """Tests that build_audit_rows returns rows sorted by filename."""
    _pw(tmp_path, "zeta.jar")
    _pw(tmp_path, "alpha.jar")
    _pw(tmp_path, "mu.jar")
    cfg = _config(tmp_path)
    rows = build_audit_rows(cfg)
    assert [r.filename for r in rows] == ["alpha.jar", "mu.jar", "zeta.jar"]


def test_build_audit_rows_with_overrides(tmp_path: Path) -> None:
    """Tests that build_audit_rows applies side overrides and review entries correctly."""
    _pw(tmp_path, "a.jar")
    _pw(tmp_path, "b.jar")
    cfg_dir = tmp_path / "config.d"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "side_overrides.toml").write_text(
        '[by_filename]\n"a.jar" = "client"\n\n[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"b.jar" = "skipped"\n', encoding="utf-8"
    )
    cfg = _config(tmp_path)
    rows = build_audit_rows(cfg)
    by_name = {r.filename: r for r in rows}
    assert by_name["a.jar"].override_section == "by_filename"
    assert by_name["a.jar"].override_value == "client"
    assert by_name["a.jar"].review_value is None
    assert by_name["b.jar"].override_section == "deployment_tool_review"
    assert by_name["b.jar"].review_value == "skipped"


def test_compute_review_entries_empty() -> None:
    """Tests that compute_review_entries returns an empty dictionary for no rows."""
    assert compute_review_entries([]) == {}


def test_compute_review_entries_maps_toggles() -> None:
    """Toggles map to expected action labels, including 'n' clearing 'c' and rows with no toggles being omitted."""
    a = _row()
    a.filename = "a.jar"
    a.toggle("s")
    b = _row()
    b.filename = "b.jar"
    b.toggle("c")
    b.toggle("n")
    c = _row()
    c.filename = "c.jar"
    d = _row()
    d.filename = "d.jar"
    d.toggle("s")
    d.toggle("c")
    assert compute_review_entries([a, b, c, d]) == {"a.jar": "server", "b.jar": "skipped", "d.jar": "both"}


def test_compute_review_entries_removes_d() -> None:
    """A row toggled with 'd' is excluded from review entries."""
    a = _row()
    a.filename = "a.jar"
    a.toggle("d")
    assert compute_review_entries([a]) == {}


def test_has_textual_is_bool() -> None:
    """Verify HAS_TEXTUAL is a boolean flag."""
    assert isinstance(HAS_TEXTUAL, bool)


@pytest.mark.skipif(not HAS_TEXTUAL, reason="textual is not installed")
def test_audit_app_class_exists() -> None:
    """Verify that AuditApp is defined in prompt_ui."""
    from minecraft.deploy_pack import prompt_ui

    assert prompt_ui.AuditApp is not None


@pytest.mark.skipif(not HAS_TEXTUAL, reason="textual is not installed")
def test_audit_app_constructs(tmp_path: Path) -> None:
    """Tests that the audit app constructs correctly."""
    from minecraft.deploy_pack import prompt_ui

    rows = [_row()]
    app = prompt_ui.AuditApp(rows)
    assert app.rows == rows
    assert app._row_by_filename["x.jar"] is rows[0]
