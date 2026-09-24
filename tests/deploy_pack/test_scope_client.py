# tests/deploy_pack/test_scope_client.py

"""Tests for deploy_pack.scope_client.

Coverage areas:
  * output filename resolution and changelog name (§7.1)
  * baseline selection: no-{date}, same-date, most-recent-different-date,
    initial build (§7.1)
  * staging: mods (with closure), config, kubejs, --with-resources (§7.1,
    §7.6)
  * ZIP layout: mods/, config/, kubejs/, resourcepacks/
  * changelog HTML written atomically
  * @www publishing for str values only; dict values skipped
  * client mods: side filter, unmarked filtered unless overridden
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from minecraft.deploy_pack.config_model import DeploymentConfig, DiscordConfig, DockerConfig, ResourcePackConfig
from minecraft.deploy_pack.deps import clear_manifest_cache
from minecraft.deploy_pack.scope_client import _changelog_name, _find_baseline, _resolve_output_name, _shared_dest_of, deploy_client_scope


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_manifest_cache()
    yield
    clear_manifest_cache()


def _config(
    tmp_path: Path, *, output_filename: str = "minecraft_client_{date}.zip", sync_mapping: dict | None = None, resource_packs: dict | None = None
) -> DeploymentConfig:
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
        compose=None,
        mods_dir_toml=None,
    )


def _write(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")


def _index(tmp_path: Path, entries: dict[str, dict]) -> None:
    """Write .pw.toml files and dummy jars.

    ``entries`` maps a filename (e.g. "a.jar") to a dict with keys
    ``side`` (str), ``id`` (str, optional). The jar file is created
    with placeholder bytes.
    """
    downloads = tmp_path / "sync" / "downloads"
    index = downloads / ".index"
    index.mkdir(parents=True, exist_ok=True)
    for filename, meta in entries.items():
        stem = filename.replace(".jar", "")
        lines = [f'filename = "{filename}"']
        if "side" in meta:
            lines.append(f'''side = "{meta["side"]}"''')
        if "name" in meta:
            lines.append(f'''name = "{meta["name"]}"''')
        (index / f"{stem}.pw.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (downloads / filename).write_bytes(b"jar:" + filename.encode())


def test_resolve_output_name_with_date() -> None:
    """Tests that _resolve_output_name replaces {date} with an 8-digit date string and preserves the resolved name in the output filename."""
    name, date_str = _resolve_output_name("minecraft_client_{date}.zip")
    assert name == f"minecraft_client_{date_str}.zip"
    assert len(date_str) == 8
    assert date_str.isdigit()


def test_resolve_output_name_no_date() -> None:
    """Tests resolving an output name without a date token in the filename."""
    name, date_str = _resolve_output_name("pack.zip")
    assert name == "pack.zip"
    assert len(date_str) == 8


def test_changelog_name_replaces_zip_suffix() -> None:
    """Tests that the changelog name replaces a .zip suffix with .html."""
    assert _changelog_name("minecraft_client_20260922.zip") == "minecraft_client_20260922.html"


def test_changelog_name_no_zip_suffix() -> None:
    """Tests generating a changelog name when the input has no .zip suffix."""
    assert _changelog_name("pack") == "pack.html"


def test_baseline_no_date_token_same_file(tmp_path: Path) -> None:
    """Tests finding a baseline when the filename has no date token and matches the same file."""
    www = tmp_path / "www"
    www.mkdir()
    (www / "pack.zip").write_bytes(b"x")
    baseline = _find_baseline(www, "pack.zip", "20260922")
    assert baseline == www / "pack.zip"


def test_baseline_no_date_token_absent(tmp_path: Path) -> None:
    """Tests that no baseline is found when the date-specific token is absent and no matching file exists."""
    www = tmp_path / "www"
    www.mkdir()
    assert _find_baseline(www, "pack.zip", "20260922") is None


def test_baseline_same_date_wins(tmp_path: Path) -> None:
    """Tests that a baseline matching the target date is selected when multiple dates exist."""
    www = tmp_path / "www"
    www.mkdir()
    (www / "minecraft_client_20260921.zip").write_bytes(b"old")
    (www / "minecraft_client_20260922.zip").write_bytes(b"today")
    baseline = _find_baseline(www, "minecraft_client_{date}.zip", "20260922")
    assert baseline == www / "minecraft_client_20260922.zip"


def test_baseline_most_recent_different_date(tmp_path: Path) -> None:
    """Tests that the baseline selects the most recent archive with a date different from the target date."""
    www = tmp_path / "www"
    www.mkdir()
    (www / "minecraft_client_20260919.zip").write_bytes(b"x")
    (www / "minecraft_client_20260921.zip").write_bytes(b"y")
    (www / "minecraft_client_20260920.zip").write_bytes(b"z")
    baseline = _find_baseline(www, "minecraft_client_{date}.zip", "20260922")
    assert baseline == www / "minecraft_client_20260921.zip"


def test_baseline_initial_build(tmp_path: Path) -> None:
    """Tests that finding a baseline returns None when the directory contains no matching archives."""
    www = tmp_path / "www"
    www.mkdir()
    assert _find_baseline(www, "minecraft_client_{date}.zip", "20260922") is None


def test_baseline_ignores_non_matching(tmp_path: Path) -> None:
    """Tests that _find_baseline ignores files whose names do not match the expected pattern."""
    www = tmp_path / "www"
    www.mkdir()
    (www / "other.zip").write_bytes(b"x")
    (www / "minecraft_client_.zip").write_bytes(b"empty middle")
    assert _find_baseline(www, "minecraft_client_{date}.zip", "20260922") is None


def test_baseline_ignores_directories(tmp_path: Path) -> None:
    """Tests that _find_baseline ignores directories even when their names match the expected pattern."""
    www = tmp_path / "www"
    www.mkdir()
    (www / "minecraft_client_20260921.zip").mkdir()
    assert _find_baseline(www, "minecraft_client_{date}.zip", "20260922") is None


def test_shared_dest_of_string() -> None:
    """Tests that _shared_dest_of returns the input unchanged for an @www/ prefixed destination."""
    assert _shared_dest_of("@www/resourcepacks") == "@www/resourcepacks"


def test_shared_dest_of_dict_ignored() -> None:
    """Dict values are scope_resource_pack's job (§4.9)."""
    assert _shared_dest_of({"resource_pack": "@www/resourcepacks", "client": "resourcepacks"}) is None


def test_shared_dest_of_plain_path_ignored() -> None:
    """Tests that _shared_dest_of returns None for a plain path without the @www/ prefix."""
    assert _shared_dest_of("config") is None


def test_shared_dest_of_non_www_at_prefix_ignored() -> None:
    """Tests that _shared_dest_of returns None for an @-prefixed destination not under @www/."""
    assert _shared_dest_of("@mods/foo") is None


def test_deploy_client_scope_minimal(tmp_path: Path) -> None:
    """Tests that a minimal client scope deployment succeeds and produces an empty zip when no content directories contain files."""
    cfg = _config(tmp_path)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    (tmp_path / "sync" / "downloads" / ".index").mkdir(parents=True)
    result = deploy_client_scope(cfg, False, [], None)
    assert result.success
    assert result.zip_path is not None
    assert result.zip_path.is_file()
    assert result.changelog_path is not None
    assert result.changelog_path.is_file()
    assert result.zip_sha256 is not None
    with zipfile.ZipFile(result.zip_path) as zf:
        assert zf.namelist() == []


def test_deploy_client_scope_with_mods(tmp_path: Path) -> None:
    """Tests that client scope deployment includes client-side and both-side mods while excluding server-only mods."""
    _index(tmp_path, {"a.jar": {"side": "client"}, "b.jar": {"side": "server"}, "c.jar": {"side": "both"}})
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path)
    result = deploy_client_scope(cfg, False, [], None)
    assert result.success
    with zipfile.ZipFile(result.zip_path) as zf:
        names = set(zf.namelist())
    assert "mods/a.jar" in names
    assert "mods/c.jar" in names
    assert "mods/b.jar" not in names


def test_deploy_client_scope_config_kubejs(tmp_path: Path) -> None:
    """Tests that client scope deployment includes files from the config and kubejs directories."""
    (tmp_path / "sync" / "downloads" / ".index").mkdir(parents=True)
    _write(tmp_path / "sync", {"config/mod.toml": "cfg", "kubejs/server_scripts/craft.js": "code"})
    cfg = _config(tmp_path)
    result = deploy_client_scope(cfg, False, [], None)
    assert result.success
    with zipfile.ZipFile(result.zip_path) as zf:
        names = set(zf.namelist())
    assert "config/mod.toml" in names
    assert "kubejs/server_scripts/craft.js" in names


def test_deploy_client_scope_closure(tmp_path: Path) -> None:
    """A client mod's mandatory dep on a server-declared jar is included."""
    downloads = tmp_path / "sync" / "downloads"
    index = downloads / ".index"
    index.mkdir(parents=True)

    with zipfile.ZipFile(downloads / "a.jar", "w") as zf:
        zf.writestr("META-INF/mods.toml", '[[mods]]\nmodId = "a"\n[[dependencies.a]]\nmodId = "lib"\nmandatory = true\nside = "BOTH"\n')
    with zipfile.ZipFile(downloads / "lib.jar", "w") as zf:
        zf.writestr("META-INF/mods.toml", '[[mods]]\nmodId = "lib"\n')
    (index / "a.pw.toml").write_text('filename = "a.jar"\nside = "client"\n', encoding="utf-8")
    (index / "lib.pw.toml").write_text('filename = "lib.jar"\nside = "server"\n', encoding="utf-8")
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path)
    result = deploy_client_scope(cfg, False, [], None)
    assert result.success
    with zipfile.ZipFile(result.zip_path) as zf:
        names = set(zf.namelist())
    assert "mods/a.jar" in names
    assert "mods/lib.jar" in names


def test_with_resources_includes_named_files(tmp_path: Path) -> None:
    """Tests that named resource pack files are included in the zip when resources are enabled."""
    (tmp_path / "sync" / "downloads" / ".index").mkdir(parents=True)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    _write(tmp_path / "sync", {"resourcepacks/pack.zip": b"ZIPDATA"})
    cfg = _config(
        tmp_path,
        sync_mapping={"config": "config", "kubejs": "kubejs", "resourcepacks": {"resource_pack": "@www/resourcepacks", "client": "resourcepacks"}},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
    )
    result = deploy_client_scope(cfg, True, [], None)
    assert result.success
    with zipfile.ZipFile(result.zip_path) as zf:
        names = set(zf.namelist())
    assert "resourcepacks/pack.zip" in names


def test_without_resources_excludes_rp(tmp_path: Path) -> None:
    """Tests that resource packs are excluded from the zip when resources are disabled."""
    (tmp_path / "sync" / "downloads" / ".index").mkdir(parents=True)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    _write(tmp_path / "sync", {"resourcepacks/pack.zip": b"ZIPDATA"})
    cfg = _config(
        tmp_path,
        sync_mapping={"config": "config", "kubejs": "kubejs", "resourcepacks": {"resource_pack": "@www/resourcepacks", "client": "resourcepacks"}},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
    )
    result = deploy_client_scope(cfg, False, [], None)
    assert result.success
    with zipfile.ZipFile(result.zip_path) as zf:
        names = set(zf.namelist())
    assert "resourcepacks/pack.zip" not in names


def test_with_resources_deduplicates(tmp_path: Path) -> None:
    """Tests that duplicate resource pack files are deduplicated in the deployed zip."""
    (tmp_path / "sync" / "downloads" / ".index").mkdir(parents=True)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    _write(tmp_path / "sync", {"resourcepacks/shared.zip": b"X"})
    cfg = _config(
        tmp_path,
        sync_mapping={"config": "config", "kubejs": "kubejs", "resourcepacks": {"resource_pack": "@www/resourcepacks", "client": "resourcepacks"}},
        resource_packs={
            "survival": ResourcePackConfig(filename="shared.zip", required=True, prompt=""),
            "creative": ResourcePackConfig(filename="shared.zip", required=False, prompt=""),
        },
    )
    result = deploy_client_scope(cfg, True, [], None)
    with zipfile.ZipFile(result.zip_path) as zf:
        assert zf.namelist().count("resourcepacks/shared.zip") == 1


def test_shared_item_published(tmp_path: Path) -> None:
    """Tests that a shared item is published to the www scope with the @-prefixed mapping."""
    (tmp_path / "sync" / "downloads" / ".index").mkdir(parents=True)
    _write(tmp_path / "sync", {"shared/foo.txt": "hello", "shared/sub/bar.txt": "world", "config/c.toml": "x"})
    cfg = _config(tmp_path, sync_mapping={"config": "config", "kubejs": "kubejs", "shared": "@www/shared"})
    result = deploy_client_scope(cfg, False, [], None)
    assert result.success
    assert (tmp_path / "www" / "shared" / "foo.txt").read_text() == "hello"
    assert (tmp_path / "www" / "shared" / "sub" / "bar.txt").read_text() == "world"
    assert any(p == tmp_path / "www" / "shared" for p in result.published_shared)


def test_shared_item_not_in_zip(tmp_path: Path) -> None:
    """Tests that shared items are excluded from the deployment zip."""
    (tmp_path / "sync" / "downloads" / ".index").mkdir(parents=True)
    _write(tmp_path / "sync", {"shared/foo.txt": "x"})
    cfg = _config(tmp_path, sync_mapping={"config": "config", "kubejs": "kubejs", "shared": "@www/shared"})
    result = deploy_client_scope(cfg, False, [], None)
    with zipfile.ZipFile(result.zip_path) as zf:
        assert not any("shared" in n for n in zf.namelist())


def test_resourcepacks_dict_not_published_by_client(tmp_path: Path) -> None:
    """Dict values are RP scope's concern (§4.9)."""
    (tmp_path / "sync" / "downloads" / ".index").mkdir(parents=True)
    _write(tmp_path / "sync", {"resourcepacks/pack.zip": b"x"})
    cfg = _config(
        tmp_path, sync_mapping={"config": "config", "kubejs": "kubejs", "resourcepacks": {"resource_pack": "@www/resourcepacks", "client": "resourcepacks"}}
    )
    deploy_client_scope(cfg, False, [], None)
    assert (tmp_path / "www" / "resourcepacks").exists() is False


def test_changelog_initial_build(tmp_path: Path) -> None:
    """Tests that the changelog is marked as an initial build."""
    (tmp_path / "sync" / "downloads" / ".index").mkdir(parents=True)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path)
    result = deploy_client_scope(cfg, False, [], None)
    assert result.initial_build is True
    html = result.changelog_path.read_text(encoding="utf-8")
    assert "Initial build" in html


def test_changelog_diff_after_second_run(tmp_path: Path) -> None:
    """A second run produces a diff, not an initial build."""
    _index(tmp_path, {"a.jar": {"side": "client"}})
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path)
    first = deploy_client_scope(cfg, False, [], None)
    assert first.initial_build is True
    downloads = tmp_path / "sync" / "downloads"
    (downloads / "b.jar").write_bytes(b"jar:b.jar")
    (downloads / ".index" / "b.pw.toml").write_text('filename = "b.jar"\nside = "client"\n', encoding="utf-8")
    second = deploy_client_scope(cfg, False, [], None)
    assert second.initial_build is False
    assert second.report is not None
    assert "b.jar" in second.report.added_mods


def test_client_scope_no_www_dir(tmp_path: Path) -> None:
    """Tests that deployment fails when www_dir is None."""
    cfg = _config(tmp_path)
    cfg = DeploymentConfig(**{**cfg.__dict__, "www_dir": None})
    result = deploy_client_scope(cfg, False, [], None)
    assert not result.success
    assert result.failure_message is not None
    assert "www_dir" in result.failure_message


def test_unmarked_side_raw_excluded(tmp_path: Path) -> None:
    """A .pw.toml with a side outside {client, server, both} is skipped."""
    downloads = tmp_path / "sync" / "downloads"
    index = downloads / ".index"
    index.mkdir(parents=True)
    (index / "a.pw.toml").write_text('filename = "a.jar"\nside = "skipped"\n', encoding="utf-8")
    (downloads / "a.jar").write_bytes(b"x")
    (tmp_path / "sync" / "config").mkdir()
    (tmp_path / "sync" / "kubejs").mkdir()
    cfg = _config(tmp_path)
    result = deploy_client_scope(cfg, False, [], None)
    with zipfile.ZipFile(result.zip_path) as zf:
        assert "mods/a.jar" not in zf.namelist()


def test_unmarked_with_override_included(tmp_path: Path) -> None:
    """An override on an otherwise-unmarked entry makes it marked."""
    downloads = tmp_path / "sync" / "downloads"
    index = downloads / ".index"
    index.mkdir(parents=True)
    (index / "a.pw.toml").write_text('filename = "a.jar"\nside = "skipped"\n', encoding="utf-8")
    (downloads / "a.jar").write_bytes(b"x")
    (tmp_path / "sync" / "config").mkdir()
    (tmp_path / "sync" / "kubejs").mkdir()
    cfg_dir = tmp_path / "config.d"
    cfg_dir.mkdir()
    (cfg_dir / "side_overrides.toml").write_text('[by_filename]\n"a.jar" = "client"\n', encoding="utf-8")
    cfg = _config(tmp_path)
    result = deploy_client_scope(cfg, False, [], None)
    with zipfile.ZipFile(result.zip_path) as zf:
        assert "mods/a.jar" in zf.namelist()
