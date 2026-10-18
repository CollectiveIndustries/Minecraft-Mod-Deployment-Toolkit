# tests/modpack_upgrade/test_seed.py

"""Seed loader tests (Manifest_Schema Section 6, Resolution_Report Section 2)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.seed import load_seed


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    """Create index_dir and modpack_dir; return (index_dir, modpack_dir)."""
    modpack_dir = tmp_path / "downloads"
    index_dir = modpack_dir / ".index"
    index_dir.mkdir(parents=True)
    return (index_dir, modpack_dir)


def _write_pw_toml(index_dir: Path, stem: str, body: str) -> None:
    """Write a .pw.toml under index_dir."""
    (index_dir / f"{stem}.pw.toml").write_text(body, encoding="utf-8")


def _write_jar(modpack_dir: Path, name: str, content: bytes = b"jar-content") -> str:
    """Write a jar under modpack_dir; return its SHA-256."""
    (modpack_dir / name).write_bytes(content)
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_empty_index_returns_empty_seed(tmp_path: Path) -> None:
    """An empty index directory yields an empty seed."""
    index_dir, modpack_dir = _setup(tmp_path)
    seed = load_seed(index_dir, modpack_dir, [])
    assert seed.report_entries == ()
    assert seed.snapshot_entries == ()


def test_external_modrinth_entry(tmp_path: Path) -> None:
    """A Modrinth-sourced seed entry is classified correctly."""
    index_dir, modpack_dir = _setup(tmp_path)
    sha = _write_jar(modpack_dir, "create.jar")
    _write_pw_toml(
        index_dir,
        "create",
        'filename = "create.jar"\nside = "both"\n[update.modrinth]\nmod-id = "create"\nversion = "v1"\n',
    )
    seed = load_seed(index_dir, modpack_dir, [])
    assert len(seed.report_entries) == 1
    entry = seed.report_entries[0]
    assert entry.filename == "create.jar"
    assert entry.provider == "modrinth"
    assert entry.jar_sha256 == sha
    assert entry.project_id == "create"
    assert entry.current_version_id == "v1"


def test_external_curseforge_entry(tmp_path: Path) -> None:
    """A CurseForge-sourced seed entry is classified correctly."""
    index_dir, modpack_dir = _setup(tmp_path)
    _write_jar(modpack_dir, "cf.jar")
    _write_pw_toml(
        index_dir,
        "cf",
        'filename = "cf.jar"\nside = "server"\n[update.curseforge]\nproject-id = 123\nfile-id = 456\n',
    )
    seed = load_seed(index_dir, modpack_dir, [])
    entry = seed.report_entries[0]
    assert entry.provider == "curseforge"
    assert entry.project_id == "123"
    assert entry.current_version_id == "456"


def test_in_house_entry_classified_by_filename(tmp_path: Path) -> None:
    """A filename in the in-house list is classified as 'in-house'."""
    index_dir, modpack_dir = _setup(tmp_path)
    _write_jar(modpack_dir, "custom.jar")
    _write_pw_toml(
        index_dir,
        "custom",
        'filename = "custom.jar"\nside = "both"\n[update.modrinth]\nmod-id = "something"\nversion = "v1"\n',
    )
    seed = load_seed(index_dir, modpack_dir, ["custom.jar"])
    entry = seed.report_entries[0]
    assert entry.provider == "in-house"
    assert entry.project_id is None
    assert entry.current_version_id is None


def test_unknown_provider_when_no_update_block(tmp_path: Path) -> None:
    """An entry with no update block is classified as 'unknown'."""
    index_dir, modpack_dir = _setup(tmp_path)
    _write_jar(modpack_dir, "orphan.jar")
    _write_pw_toml(
        index_dir,
        "orphan",
        'filename = "orphan.jar"\nside = "both"\n',
    )
    seed = load_seed(index_dir, modpack_dir, [])
    assert seed.report_entries[0].provider == "unknown"


# ---------------------------------------------------------------------------
# Side handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("side", ["client", "server", "both", "unknown"])
def test_valid_side_accepted(tmp_path: Path, side: str) -> None:
    """Every documented side value is accepted."""
    index_dir, modpack_dir = _setup(tmp_path)
    _write_jar(modpack_dir, "a.jar")
    _write_pw_toml(index_dir, "a", f'filename = "a.jar"\nside = "{side}"\n')
    seed = load_seed(index_dir, modpack_dir, [])
    assert seed.report_entries[0].side_declared == side


def test_missing_side_defaults_to_unknown(tmp_path: Path) -> None:
    """An absent side defaults to 'unknown'."""
    index_dir, modpack_dir = _setup(tmp_path)
    _write_jar(modpack_dir, "a.jar")
    _write_pw_toml(index_dir, "a", 'filename = "a.jar"\n')
    seed = load_seed(index_dir, modpack_dir, [])
    assert seed.report_entries[0].side_declared == "unknown"


def test_snapshot_side_empty_string_for_unknown(tmp_path: Path) -> None:
    """Section A2.2: side_declared in the snapshot is "" when the side is 'unknown'."""
    index_dir, modpack_dir = _setup(tmp_path)
    _write_jar(modpack_dir, "a.jar")
    _write_pw_toml(index_dir, "a", 'filename = "a.jar"\nside = "unknown"\n')
    seed = load_seed(index_dir, modpack_dir, [])
    assert seed.snapshot_entries[0].side_declared == ""


def test_non_string_side_raises(tmp_path: Path) -> None:
    """A non-string side raises SchemaError."""
    index_dir, modpack_dir = _setup(tmp_path)
    _write_jar(modpack_dir, "a.jar")
    _write_pw_toml(index_dir, "a", 'filename = "a.jar"\nside = 42\n')
    with pytest.raises(SchemaError, match="side must be a string"):
        load_seed(index_dir, modpack_dir, [])


def test_invalid_string_side_raises(tmp_path: Path) -> None:
    """A string outside the enum raises SchemaError."""
    index_dir, modpack_dir = _setup(tmp_path)
    _write_jar(modpack_dir, "a.jar")
    _write_pw_toml(index_dir, "a", 'filename = "a.jar"\nside = "bogus"\n')
    with pytest.raises(SchemaError, match="not one of"):
        load_seed(index_dir, modpack_dir, [])


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_index_dir_raises(tmp_path: Path) -> None:
    """A missing index directory raises SchemaError."""
    modpack_dir = tmp_path / "downloads"
    modpack_dir.mkdir()
    with pytest.raises(SchemaError, match="not found"):
        load_seed(modpack_dir / ".index", modpack_dir, [])


def test_malformed_toml_raises(tmp_path: Path) -> None:
    """Malformed .pw.toml raises SchemaError."""
    index_dir, modpack_dir = _setup(tmp_path)
    _write_pw_toml(index_dir, "a", "not = valid = toml\n")
    with pytest.raises(SchemaError, match="could not parse"):
        load_seed(index_dir, modpack_dir, [])


def test_missing_filename_raises(tmp_path: Path) -> None:
    """A .pw.toml without a filename raises SchemaError."""
    index_dir, modpack_dir = _setup(tmp_path)
    _write_pw_toml(index_dir, "a", 'side = "both"\n')
    with pytest.raises(SchemaError, match="filename is required"):
        load_seed(index_dir, modpack_dir, [])


def test_missing_jar_raises(tmp_path: Path) -> None:
    """A .pw.toml referencing a missing jar raises SchemaError."""
    index_dir, modpack_dir = _setup(tmp_path)
    _write_pw_toml(index_dir, "a", 'filename = "missing.jar"\nside = "both"\n')
    with pytest.raises(SchemaError, match="seed jar missing"):
        load_seed(index_dir, modpack_dir, [])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def test_dependencies_parsed_and_sorted(tmp_path: Path) -> None:
    """[[x-prismlauncher-dependencies]] blocks are parsed and sorted."""
    index_dir, modpack_dir = _setup(tmp_path)
    _write_jar(modpack_dir, "a.jar")
    _write_pw_toml(
        index_dir,
        "a",
        'filename = "a.jar"\nside = "both"\n'
        '[[x-prismlauncher-dependencies]]\naddonId = "zeta"\ntype = "required"\n'
        '[[x-prismlauncher-dependencies]]\naddonId = "alpha"\ntype = "optional"\n',
    )
    seed = load_seed(index_dir, modpack_dir, [])
    deps = seed.snapshot_entries[0].dependencies
    assert [d.addon_id for d in deps] == ["alpha", "zeta"]
