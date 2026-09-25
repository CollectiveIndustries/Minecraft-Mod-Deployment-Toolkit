# tests/deploy_pack/test_scope_client.py

"""Tests for deploy_pack.scope_client, Project_Specs.md §1.1, §2.1, §7.1, §7.5, §7.6.

Coverage:

  * §7.1 - output filename resolution; changelog HTML name; ZIP layout
  * §7.1 - baseline selection: no-{date}, same-date, most-recent-
           different-date, initial build
  * §7.6 - --with-resources stages the union of [resource_pack.X]
           filenames
  * §2.1 - @www/* shared items are published separately from the ZIP
  * §1.1 - the changelog HTML is produced by common/changelog.py

Every test drives the scope through :func:`deploy_client_scope`. The
baseline logic and output filename resolution are spec-defined but
currently live behind private helpers; the tests pin their observable
behavior by inspecting the ZIP and the changelog HTML on disk.
"""

from __future__ import annotations

import datetime
import zipfile
from pathlib import Path

import pytest

from minecraft.deploy_pack.config_model import (
    ComposeLoadResult,
    DeploymentConfig,
    DiscordConfig,
    DockerConfig,
    ResourcePackConfig,
)
from minecraft.deploy_pack.deps import clear_manifest_cache
from minecraft.deploy_pack.scope_client import deploy_client_scope


@pytest.fixture(autouse=True)
def _clear_manifest_cache() -> None:
    """Reset the per-modpack manifest cache around every test."""
    clear_manifest_cache()
    yield
    clear_manifest_cache()


# ---------------------------------------------------------------------------
# Config builder
# ---------------------------------------------------------------------------


def _config(
    tmp_path: Path,
    *,
    output_filename: str = "minecraft_client_{date}.zip",
    sync_mapping: dict | None = None,
    resource_packs: dict | None = None,
) -> DeploymentConfig:
    """Build a DeploymentConfig with sane defaults for client-scope tests."""
    www = tmp_path / "www"
    www.mkdir(exist_ok=True)
    return DeploymentConfig(
        project_root=tmp_path,
        config_dir=tmp_path / "config.d",
        sync_root=tmp_path / "sync",
        modpack_dir=tmp_path / "sync" / "downloads",
        www_dir=www,
        www_dir_error=None,
        www_dir_candidates=[],
        output_filename=output_filename,
        download_base_url="http://minecraft/downloads",
        protect_file=None,
        sync_mapping=sync_mapping if sync_mapping is not None else {"config": "config", "kubejs": "kubejs"},
        restart_policy={},
        instances={},
        partition=[],
        partition_unknown=[],
        requested_instances=None,
        resource_packs=resource_packs or {},
        docker=DockerConfig(compose_file=tmp_path / "docker-compose.yml"),
        discord=DiscordConfig(),
        webhook_url=None,
        compose=ComposeLoadResult(file=None, error="not used by these tests"),
        mods_dir_toml=None,
    )


def _write_tree(root: Path, files: dict[str, str | bytes]) -> None:
    """Populate a directory tree from a {relative_path: content} mapping."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")


def _write_index(tmp_path: Path, entries: dict[str, str]) -> None:
    """Write .pw.toml files and placeholder jars for filename -> side."""
    idx = tmp_path / "sync" / "downloads" / ".index"
    idx.mkdir(parents=True, exist_ok=True)
    for filename, side in entries.items():
        stem = filename.replace(".jar", "")
        (idx / f"{stem}.pw.toml").write_text(
            f'filename = "{filename}"\nside = "{side}"\n',
            encoding="utf-8",
        )
        (tmp_path / "sync" / "downloads" / filename).write_bytes(b"jar:" + filename.encode())


def _zip_names(zip_path: Path) -> set[str]:
    """Return the set of names in a ZIP file."""
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


# ---------------------------------------------------------------------------
# §7.1: output filename and changelog name
# ---------------------------------------------------------------------------


def test_output_filename_with_date_token_is_resolved(tmp_path: Path) -> None:
    """§7.1: {date} is replaced with today's UTC date in YYYYMMDD form."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg = _config(tmp_path, output_filename="minecraft_client_{date}.zip")
    result = deploy_client_scope(cfg, False, [], None)
    assert result.success
    today = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")
    assert result.resolved_output_filename == f"minecraft_client_{today}.zip"
    assert result.zip_path is not None
    assert result.zip_path.name == f"minecraft_client_{today}.zip"


def test_output_filename_without_date_token_is_used_verbatim(tmp_path: Path) -> None:
    """§7.1: a template without {date} produces the same filename every run."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg = _config(tmp_path, output_filename="pack.zip")
    result = deploy_client_scope(cfg, False, [], None)
    assert result.resolved_output_filename == "pack.zip"


def test_changelog_name_replaces_zip_suffix_with_html(tmp_path: Path) -> None:
    """§7.1: the changelog HTML filename is the ZIP name with .zip replaced by .html."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg = _config(tmp_path, output_filename="pack.zip")
    result = deploy_client_scope(cfg, False, [], None)
    assert result.changelog_path is not None
    assert result.changelog_path.name == "pack.html"


# ---------------------------------------------------------------------------
# §7.1: ZIP contents
# ---------------------------------------------------------------------------


def test_zip_is_built_when_source_dirs_are_empty(tmp_path: Path) -> None:
    """§7.1: an empty staging tree produces an empty ZIP; the ZIP is still built."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg = _config(tmp_path)
    result = deploy_client_scope(cfg, False, [], None)
    assert result.success
    assert _zip_names(result.zip_path) == set()


def test_zip_contains_config_and_kubejs_files(tmp_path: Path) -> None:
    """§7.1: config/** and kubejs/** from sync_root are staged into the ZIP."""
    _write_tree(
        tmp_path / "sync",
        {
            "config/mod.toml": "cfg",
            "kubejs/server_scripts/craft.js": "code",
        },
    )
    cfg = _config(tmp_path)
    result = deploy_client_scope(cfg, False, [], None)
    names = _zip_names(result.zip_path)
    assert "config/mod.toml" in names
    assert "kubejs/server_scripts/craft.js" in names


def test_zip_contains_client_side_mods_only(tmp_path: Path) -> None:
    """§7.1: only client-side and both-side mods are staged into the ZIP."""
    _write_index(tmp_path, {"client.jar": "client", "server.jar": "server", "both.jar": "both"})
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg = _config(tmp_path)
    result = deploy_client_scope(cfg, False, [], None)
    names = _zip_names(result.zip_path)
    assert "mods/client.jar" in names
    assert "mods/both.jar" in names
    assert "mods/server.jar" not in names


# ---------------------------------------------------------------------------
# §7.6: --with-resources stages resource packs
# ---------------------------------------------------------------------------


def _rp_config(tmp_path: Path, filenames: list[str]) -> DeploymentConfig:
    """Return a config wired for --with-resources with the given pack filenames."""
    _write_tree(tmp_path / "sync" / "resourcepacks", dict.fromkeys(filenames, b"ZIPDATA"))
    rp_map = {"survival": ResourcePackConfig(filename=filenames[0], required=True, prompt="")}
    for idx, name in enumerate(filenames[1:], start=1):
        rp_map[f"extra{idx}"] = ResourcePackConfig(filename=name, required=False, prompt="")
    return _config(
        tmp_path,
        sync_mapping={
            "config": "config",
            "kubejs": "kubejs",
            "resourcepacks": {"resource_pack": "@www/resourcepacks", "client": "resourcepacks"},
        },
        resource_packs=rp_map,
    )


def test_with_resources_includes_named_resource_packs(tmp_path: Path) -> None:
    """§7.6: --with-resources stages the union of [resource_pack.X].filename values."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg = _rp_config(tmp_path, ["pack.zip"])
    result = deploy_client_scope(cfg, True, [], None)
    names = _zip_names(result.zip_path)
    assert "resourcepacks/pack.zip" in names


def test_without_resources_excludes_resource_packs(tmp_path: Path) -> None:
    """§7.6: without --with-resources, no resource packs are staged."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg = _rp_config(tmp_path, ["pack.zip"])
    result = deploy_client_scope(cfg, False, [], None)
    names = _zip_names(result.zip_path)
    assert "resourcepacks/pack.zip" not in names


def test_with_resources_deduplicates_identical_filenames(tmp_path: Path) -> None:
    """§7.6: duplicate filenames appear once in the ZIP."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    _write_tree(tmp_path / "sync" / "resourcepacks", {"shared.zip": b"X"})
    cfg = _config(
        tmp_path,
        sync_mapping={
            "config": "config",
            "kubejs": "kubejs",
            "resourcepacks": {"resource_pack": "@www/resourcepacks", "client": "resourcepacks"},
        },
        resource_packs={
            "survival": ResourcePackConfig(filename="shared.zip", required=True, prompt=""),
            "creative": ResourcePackConfig(filename="shared.zip", required=False, prompt=""),
        },
    )
    result = deploy_client_scope(cfg, True, [], None)
    with zipfile.ZipFile(result.zip_path) as zf:
        assert zf.namelist().count("resourcepacks/shared.zip") == 1


# ---------------------------------------------------------------------------
# §2.1: @www shared-item publication
# ---------------------------------------------------------------------------


def test_shared_item_published_to_www_dir(tmp_path: Path) -> None:
    """§2.1: sync-mapping entries whose value starts with @www/ are copied to www_dir."""
    _write_tree(tmp_path / "sync", {"shared/foo.txt": "hello", "shared/sub/bar.txt": "world"})
    cfg = _config(
        tmp_path,
        sync_mapping={"config": "config", "kubejs": "kubejs", "shared": "@www/shared"},
    )
    result = deploy_client_scope(cfg, False, [], None)
    assert (tmp_path / "www" / "shared" / "foo.txt").read_text(encoding="utf-8") == "hello"
    assert (tmp_path / "www" / "shared" / "sub" / "bar.txt").read_text(encoding="utf-8") == "world"
    assert (tmp_path / "www" / "shared") in result.published_shared


def test_shared_item_is_not_embedded_in_the_zip(tmp_path: Path) -> None:
    """§2.1: @www/* items are published separately; they do not appear in the client ZIP."""
    _write_tree(tmp_path / "sync", {"shared/foo.txt": "x"})
    cfg = _config(
        tmp_path,
        sync_mapping={"config": "config", "kubejs": "kubejs", "shared": "@www/shared"},
    )
    result = deploy_client_scope(cfg, False, [], None)
    names = _zip_names(result.zip_path)
    assert not any("shared" in n for n in names)


def test_resourcepacks_dict_value_is_not_published_by_client_scope(tmp_path: Path) -> None:
    """§4.9: dict-valued resourcepacks entries are the resource-pack scope's concern."""
    _write_tree(tmp_path / "sync", {"resourcepacks/pack.zip": b"x"})
    cfg = _config(
        tmp_path,
        sync_mapping={
            "config": "config",
            "kubejs": "kubejs",
            "resourcepacks": {"resource_pack": "@www/resourcepacks", "client": "resourcepacks"},
        },
    )
    deploy_client_scope(cfg, False, [], None)
    assert not (tmp_path / "www" / "resourcepacks").exists()


# ---------------------------------------------------------------------------
# §7.1: baseline selection
# ---------------------------------------------------------------------------


def _read_changelog(html_path: Path) -> str:
    """Return the changelog HTML as text."""
    return html_path.read_text(encoding="utf-8")


def test_first_build_reports_initial_build(tmp_path: Path) -> None:
    """§7.1: with no prior ZIP, the changelog reports an initial build."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg = _config(tmp_path)
    result = deploy_client_scope(cfg, False, [], None)
    assert result.initial_build is True
    assert "Initial build" in _read_changelog(result.changelog_path)


def test_same_date_rerun_uses_the_existing_zip_as_baseline(tmp_path: Path) -> None:
    """§7.1: a same-date ZIP is the baseline for a re-run."""
    _write_index(tmp_path, {"a.jar": "client"})
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg = _config(tmp_path)
    first = deploy_client_scope(cfg, False, [], None)
    assert first.initial_build is True
    second = deploy_client_scope(cfg, False, [], None)
    assert second.initial_build is False
    assert second.baseline_zip == first.zip_path


def test_new_date_uses_most_recent_prior_zip_as_baseline(tmp_path: Path) -> None:
    """§7.1: when no same-date ZIP exists, the baseline is the most recent prior dated ZIP."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    www = tmp_path / "www"
    www.mkdir(exist_ok=True)
    (www / "minecraft_client_20200101.zip").write_bytes(b"old")
    (www / "minecraft_client_20200103.zip").write_bytes(b"newer")
    (www / "minecraft_client_20200102.zip").write_bytes(b"older")
    cfg = _config(tmp_path, output_filename="minecraft_client_{date}.zip")
    result = deploy_client_scope(cfg, False, [], None)
    assert result.initial_build is False
    assert result.baseline_zip is not None
    assert result.baseline_zip.name == "minecraft_client_20200103.zip"


def test_no_date_token_uses_the_existing_file_as_baseline(tmp_path: Path) -> None:
    """§7.1: a template without {date} uses the existing file at the resolved path."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    www = tmp_path / "www"
    www.mkdir(exist_ok=True)
    (www / "pack.zip").write_bytes(b"existing")
    cfg = _config(tmp_path, output_filename="pack.zip")
    result = deploy_client_scope(cfg, False, [], None)
    assert result.initial_build is False
    assert result.baseline_zip == www / "pack.zip"


def test_changelog_lists_added_mods_when_a_diff_exists(tmp_path: Path) -> None:
    """§1.1: added mods are listed in the changelog HTML after a second run."""
    _write_index(tmp_path, {"a.jar": "client"})
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg = _config(tmp_path)
    deploy_client_scope(cfg, False, [], None)
    _write_index(tmp_path, {"b.jar": "client"})
    second = deploy_client_scope(cfg, False, [], None)
    assert second.initial_build is False
    assert second.report is not None
    assert "b.jar" in second.report.added_mods


# ---------------------------------------------------------------------------
# §2.6 / §7.1: no www_dir is a failure
# ---------------------------------------------------------------------------


def test_missing_www_dir_is_a_failure(tmp_path: Path) -> None:
    """§7.1: the client scope requires www_dir; without it, deployment cannot proceed."""
    cfg = _config(tmp_path)
    cfg = DeploymentConfig(**{**cfg.__dict__, "www_dir": None})
    result = deploy_client_scope(cfg, False, [], None)
    assert not result.success
    assert result.failure_message is not None
    assert "www_dir" in result.failure_message


# ---------------------------------------------------------------------------
# §7.1: atomic publication
# ---------------------------------------------------------------------------


def test_zip_is_replaced_atomically_on_second_run(tmp_path: Path) -> None:
    """§4.10: a second run replaces the ZIP without leaving a partial artifact behind."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg = _config(tmp_path)
    first = deploy_client_scope(cfg, False, [], None)
    _write_tree(tmp_path / "sync", {"config/new.toml": "x"})
    second = deploy_client_scope(cfg, False, [], None)
    assert first.zip_path == second.zip_path
    names = _zip_names(second.zip_path)
    assert "config/new.toml" in names


def test_zip_sha256_is_recorded(tmp_path: Path) -> None:
    """§5.4: the ZIP's SHA-256 is recorded on the result so it can be advertised."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg = _config(tmp_path)
    result = deploy_client_scope(cfg, False, [], None)
    assert result.zip_sha256 is not None
    assert len(result.zip_sha256) == 64  # SHA-256 hex length
