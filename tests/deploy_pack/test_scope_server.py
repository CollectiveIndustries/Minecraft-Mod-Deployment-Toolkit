# tests/deploy_pack/test_scope_server.py

"""Tests for deploy_pack.scope_server, Project_Specs.md §2.9, §4.2, §4.11, §6.3, §7.2.

Coverage:

  * §2.9  - targeted server deploys skip mods_dir entirely
  * §4.2  - halt on first runtime failure; no rollback of completed steps
  * §4.11 - mods_dir is flat; protected files outside the source set
            survive the clean
  * §6.3  - unmarked entries (declared side outside {client, server, both})
            are dropped from the deploy set unless an override marks them
  * §7.2  - config_mode merge vs delete; kubejs_mode is always delete

Every test drives the scope through :func:`deploy_server_scope` against
a real DeploymentConfig and a real PreflightPlan. The mod-source
resolution is spec-defined (§3.11, §6.3) and lives behind a private
helper; the tests pin its observable behavior by asserting what ends up
on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import minecraft.deploy_pack.scope_server as ss
from minecraft.deploy_pack.config_model import (
    ComposeLoadResult,
    DeploymentConfig,
    DiscordConfig,
    DockerConfig,
    InstanceConfig,
)
from minecraft.deploy_pack.files import CopyResult
from minecraft.deploy_pack.preflight import PreflightPlan, ScopeSet
from minecraft.deploy_pack.scope_server import (
    MemberWriteResult,
    ServerScopeResult,
    deploy_server_scope,
)

# ---------------------------------------------------------------------------
# Config and plan builders
# ---------------------------------------------------------------------------


def _instance(
    name: str,
    root: Path,
    *,
    config_mode: str = "merge",
    kubejs_mode: str = "delete",
) -> InstanceConfig:
    """Return an InstanceConfig rooted at ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    return InstanceConfig(
        name=name,
        container=f"mc-{name}",
        config_mode=config_mode,
        kubejs_mode=kubejs_mode,
        instance_root=root,
        config_path=root / "config",
        kubejs_path=root / "kubejs",
        server_properties_path=root / "server.properties",
    )


def _config(
    tmp_path: Path,
    *,
    partition: list[str],
    instances: dict[str, InstanceConfig],
    sync_mapping: dict | None = None,
) -> DeploymentConfig:
    """Build a DeploymentConfig with the given partition and instances."""
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
        output_filename="minecraft_client_{date}.zip",
        download_base_url="http://minecraft/downloads",
        protect_file=None,
        sync_mapping=sync_mapping if sync_mapping is not None else {"config": "config", "kubejs": "kubejs"},
        restart_policy={},
        instances=instances,
        partition=partition,
        partition_unknown=[],
        requested_instances=None,
        resource_packs={},
        docker=DockerConfig(compose_file=tmp_path / "docker-compose.yml"),
        discord=DiscordConfig(),
        webhook_url=None,
        compose=ComposeLoadResult(file=None, error="not used by these tests"),
        mods_dir_toml=None,
    )


def _plan(
    *,
    partition: list[str],
    mods_dir: Path | None = None,
    targeted: bool = False,
) -> PreflightPlan:
    """Build a minimal PreflightPlan carrying the fields the scope reads."""
    return PreflightPlan(
        scopes=ScopeSet(server=True),
        partition=list(partition),
        member_plans={},
        none_set=[],
        reload_set=[],
        restart_set=[],
        pack_required=False,
        container_states={},
        mods_dir=mods_dir,
        targeted=targeted,
    )


def _write_tree(root: Path, files: dict[str, str]) -> None:
    """Populate a directory tree from a {relative_path: content} mapping."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _read_tree(root: Path) -> dict[str, str]:
    """Return {relative_path: content} for every file under ``root``."""
    if not root.is_dir():
        return {}
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file()}


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
        (tmp_path / "sync" / "downloads" / filename).write_bytes(b"jar-content")


# ---------------------------------------------------------------------------
# §4.11: mods_dir is flat
# ---------------------------------------------------------------------------


def test_mods_dir_flat_deploy_adds_new_jars(tmp_path: Path) -> None:
    """§4.11: source jars not present in mods_dir are added."""
    _write_index(tmp_path, {"a.jar": "server", "b.jar": "server"})
    _write_tree(tmp_path / "sync" / "config", {"c.toml": "x"})
    _write_tree(tmp_path / "sync" / "kubejs", {"scripts/a.js": "x"})
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir()
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", tmp_path / "survival")},
    )
    plan = _plan(partition=["survival"], mods_dir=mods_dir)
    result = deploy_server_scope(cfg, plan, [], None)
    assert result.success
    assert result.mods_result is not None
    assert sorted(result.mods_result.added) == ["a.jar", "b.jar"]
    assert (mods_dir / "a.jar").read_bytes() == b"jar-content"


def test_mods_dir_flat_removes_stale_jars(tmp_path: Path) -> None:
    """§4.11: a .jar not in the source set is removed from mods_dir."""
    _write_index(tmp_path, {"a.jar": "server"})
    _write_tree(tmp_path / "sync" / "config", {"c.toml": "x"})
    _write_tree(tmp_path / "sync" / "kubejs", {"scripts/a.js": "x"})
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir()
    (mods_dir / "stale.jar").write_bytes(b"old")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", tmp_path / "survival")},
    )
    plan = _plan(partition=["survival"], mods_dir=mods_dir)
    result = deploy_server_scope(cfg, plan, [], None)
    assert result.mods_result is not None
    assert result.mods_result.removed == ["stale.jar"]
    assert not (mods_dir / "stale.jar").exists()


def test_mods_dir_flat_ignores_non_jar_files(tmp_path: Path) -> None:
    """§4.11: only *.jar is in the effective mod set; other files are untouched."""
    _write_index(tmp_path, {"a.jar": "server"})
    _write_tree(tmp_path / "sync" / "config", {"c.toml": "x"})
    _write_tree(tmp_path / "sync" / "kubejs", {"scripts/a.js": "x"})
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir()
    (mods_dir / "readme.txt").write_text("keep", encoding="utf-8")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", tmp_path / "survival")},
    )
    plan = _plan(partition=["survival"], mods_dir=mods_dir)
    deploy_server_scope(cfg, plan, [], None)
    assert (mods_dir / "readme.txt").is_file()


def test_mods_dir_flat_protected_stale_survives(tmp_path: Path) -> None:
    """§3.13 + §4.11: a protected stale jar survives the clean."""
    _write_index(tmp_path, {"a.jar": "server"})
    _write_tree(tmp_path / "sync" / "config", {"c.toml": "x"})
    _write_tree(tmp_path / "sync" / "kubejs", {"scripts/a.js": "x"})
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir()
    (mods_dir / "keep.jar").write_bytes(b"protected")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", tmp_path / "survival")},
    )
    plan = _plan(partition=["survival"], mods_dir=mods_dir)
    result = deploy_server_scope(cfg, plan, ["keep.jar"], None)
    assert (mods_dir / "keep.jar").is_file()
    assert "keep.jar" in result.protected_kept


def test_mods_dir_flat_protected_in_source_is_overwritten(tmp_path: Path) -> None:
    """§3.13: protection governs deletion, not overwrite."""
    _write_index(tmp_path, {"keep.jar": "server"})
    _write_tree(tmp_path / "sync" / "config", {"c.toml": "x"})
    _write_tree(tmp_path / "sync" / "kubejs", {"scripts/a.js": "x"})
    (tmp_path / "sync" / "downloads" / "keep.jar").write_bytes(b"new")
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir()
    (mods_dir / "keep.jar").write_bytes(b"old")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", tmp_path / "survival")},
    )
    plan = _plan(partition=["survival"], mods_dir=mods_dir)
    deploy_server_scope(cfg, plan, ["keep.jar"], None)
    assert (mods_dir / "keep.jar").read_bytes() == b"new"


# ---------------------------------------------------------------------------
# §2.9: targeted server deploy skips mods_dir
# ---------------------------------------------------------------------------


def test_targeted_deploy_does_not_touch_mods_dir(tmp_path: Path) -> None:
    """§2.9: --server --instance X does not write to mods_dir."""
    _write_index(tmp_path, {"a.jar": "server"})
    _write_tree(tmp_path / "sync" / "config", {"c.toml": "x"})
    _write_tree(tmp_path / "sync" / "kubejs", {"scripts/a.js": "x"})
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir()
    (mods_dir / "existing.jar").write_bytes(b"pre-existing")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", tmp_path / "survival")},
    )
    plan = _plan(partition=["survival"], mods_dir=mods_dir, targeted=True)
    result = deploy_server_scope(cfg, plan, [], None)
    assert result.mods_skipped_reason is not None
    assert result.mods_result is None
    assert (mods_dir / "existing.jar").read_bytes() == b"pre-existing"
    assert not (mods_dir / "a.jar").exists()


def test_no_mods_dir_in_plan_skips_mods(tmp_path: Path) -> None:
    """§3.7: when mods_dir cannot be determined, mods are not deployed."""
    _write_tree(tmp_path / "sync" / "config", {"c.toml": "x"})
    _write_tree(tmp_path / "sync" / "kubejs", {"scripts/a.js": "x"})
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", tmp_path / "survival")},
    )
    plan = _plan(partition=["survival"], mods_dir=None)
    result = deploy_server_scope(cfg, plan, [], None)
    assert result.mods_skipped_reason is not None
    assert result.mods_result is None


# ---------------------------------------------------------------------------
# §7.2: config_mode merge vs delete
# ---------------------------------------------------------------------------


def test_config_merge_keeps_extra_files(tmp_path: Path) -> None:
    """§7.2: merge mode keeps files that exist only in the destination."""
    _write_tree(tmp_path / "sync" / "config", {"common.toml": "new"})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    root = tmp_path / "survival"
    _write_tree(root / "config", {"common.toml": "new", "extra.toml": "keep"})
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", root, config_mode="merge")},
    )
    deploy_server_scope(cfg, _plan(partition=["survival"]), [], None)
    assert (root / "config" / "extra.toml").is_file()
    assert (root / "config" / "common.toml").read_text(encoding="utf-8") == "new"


def test_config_delete_removes_extra_files(tmp_path: Path) -> None:
    """§7.2: delete mode removes files that exist only in the destination."""
    _write_tree(tmp_path / "sync" / "config", {"common.toml": "x"})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    root = tmp_path / "survival"
    _write_tree(root / "config", {"common.toml": "x", "extra.toml": "gone"})
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", root, config_mode="delete")},
    )
    result = deploy_server_scope(cfg, _plan(partition=["survival"]), [], None)
    assert not (root / "config" / "extra.toml").exists()
    member = result.member_results["survival"]
    assert member.config_result is not None
    assert "extra.toml" in member.config_result.removed


def test_config_merge_updates_changed_files_in_place(tmp_path: Path) -> None:
    """§4.4: a file present in both trees with different content is updated, not removed."""
    _write_tree(tmp_path / "sync" / "config", {"common.toml": "new"})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    root = tmp_path / "survival"
    _write_tree(root / "config", {"common.toml": "old"})
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", root, config_mode="merge")},
    )
    result = deploy_server_scope(cfg, _plan(partition=["survival"]), [], None)
    member = result.member_results["survival"]
    assert member.config_result is not None
    assert member.config_result.updated == ["common.toml"]
    assert member.config_result.removed == []


def test_config_merge_leaves_unchanged_files_untouched(tmp_path: Path) -> None:
    """§4.4: unchanged files are neither removed nor rewritten."""
    _write_tree(tmp_path / "sync" / "config", {"common.toml": "same"})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    root = tmp_path / "survival"
    _write_tree(root / "config", {"common.toml": "same"})
    before = (root / "config" / "common.toml").stat().st_mtime_ns
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", root, config_mode="merge")},
    )
    deploy_server_scope(cfg, _plan(partition=["survival"]), [], None)
    assert (root / "config" / "common.toml").stat().st_mtime_ns == before


# ---------------------------------------------------------------------------
# §7.2: kubejs_mode is always delete
# ---------------------------------------------------------------------------


def test_kubejs_always_delete_regardless_of_instance_setting(tmp_path: Path) -> None:
    """§7.2: kubejs_mode is always delete."""
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {"scripts/a.js": "new"})
    root = tmp_path / "survival"
    _write_tree(root / "kubejs", {"scripts/a.js": "new", "stale.js": "x"})
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", root, kubejs_mode="delete")},
    )
    deploy_server_scope(cfg, _plan(partition=["survival"]), [], None)
    assert not (root / "kubejs" / "stale.js").exists()


# ---------------------------------------------------------------------------
# Other sync-mapping keys default to merge
# ---------------------------------------------------------------------------


def test_unknown_sync_mapping_key_defaults_to_merge(tmp_path: Path) -> None:
    """§7.2: modes are defined only for config and kubejs; other keys default to merge."""
    _write_tree(tmp_path / "sync" / "extra", {"a.txt": "1"})
    root = tmp_path / "survival"
    _write_tree(root / "extra", {"a.txt": "1", "keep.txt": "x"})
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", root)},
        sync_mapping={"config": "config", "kubejs": "kubejs", "extra": "extra"},
    )
    result = deploy_server_scope(cfg, _plan(partition=["survival"]), [], None)
    assert (root / "extra" / "keep.txt").is_file()
    member = result.member_results["survival"]
    assert "extra" in member.other_results


# ---------------------------------------------------------------------------
# Shared destinations (@www/...) are the client scope's concern
# ---------------------------------------------------------------------------


def test_shared_dest_is_skipped_by_server_scope(tmp_path: Path) -> None:
    """§2.1: @www/* items are published by the client scope, not the server scope."""
    _write_tree(tmp_path / "sync" / "resourcepacks", {"pack.zip": "x"})
    root = tmp_path / "survival"
    root.mkdir(parents=True)
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", root)},
        sync_mapping={
            "config": "config",
            "kubejs": "kubejs",
            "resourcepacks": {"server": "@www/resourcepacks", "client": "resourcepacks"},
        },
    )
    deploy_server_scope(cfg, _plan(partition=["survival"]), [], None)
    assert not (root / "resourcepacks").exists()


# ---------------------------------------------------------------------------
# Missing source directories are silently skipped
# ---------------------------------------------------------------------------


def test_missing_source_dir_is_silently_skipped(tmp_path: Path) -> None:
    """A sync-mapping key whose source dir is absent is skipped with no result."""
    root = tmp_path / "survival"
    root.mkdir(parents=True)
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", root)},
    )
    result = deploy_server_scope(cfg, _plan(partition=["survival"]), [], None)
    assert result.success
    assert result.member_results["survival"].config_result is None


# ---------------------------------------------------------------------------
# §6.3: unmarked entries
# ---------------------------------------------------------------------------


def test_unmarked_entries_are_dropped(tmp_path: Path) -> None:
    """§6.3: an entry whose declared side is outside {client, server, both} is not deployed."""
    idx = tmp_path / "sync" / "downloads" / ".index"
    idx.mkdir(parents=True)
    (idx / "a.pw.toml").write_text('filename = "a.jar"\nside = "skipped"\n', encoding="utf-8")
    (tmp_path / "sync" / "downloads" / "a.jar").write_bytes(b"x")
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir()
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", tmp_path / "survival")},
    )
    plan = _plan(partition=["survival"], mods_dir=mods_dir)
    deploy_server_scope(cfg, plan, [], None)
    assert not (mods_dir / "a.jar").exists()


def test_unmarked_entry_with_override_is_included(tmp_path: Path) -> None:
    """§6.3: an override on an otherwise-unmarked entry makes it marked."""
    idx = tmp_path / "sync" / "downloads" / ".index"
    idx.mkdir(parents=True)
    (idx / "a.pw.toml").write_text('filename = "a.jar"\nside = "skipped"\n', encoding="utf-8")
    (tmp_path / "sync" / "downloads" / "a.jar").write_bytes(b"x")
    _write_tree(tmp_path / "sync" / "config", {})
    _write_tree(tmp_path / "sync" / "kubejs", {})
    cfg_dir = tmp_path / "config.d"
    cfg_dir.mkdir()
    (cfg_dir / "side_overrides.toml").write_text('[by_filename]\n"a.jar" = "server"\n', encoding="utf-8")
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir()
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", tmp_path / "survival")},
    )
    plan = _plan(partition=["survival"], mods_dir=mods_dir)
    deploy_server_scope(cfg, plan, [], None)
    assert (mods_dir / "a.jar").is_file()


# ---------------------------------------------------------------------------
# §4.2: halt on first failure
# ---------------------------------------------------------------------------


def test_mods_failure_halts_before_any_member_is_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§4.2: a mods failure halts the whole scope before any member is touched."""
    _write_index(tmp_path, {"a.jar": "server"})
    _write_tree(tmp_path / "sync" / "config", {"c.toml": "x"})
    _write_tree(tmp_path / "sync" / "kubejs", {"scripts/a.js": "x"})
    root = tmp_path / "survival"
    root.mkdir(parents=True)
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", root)},
    )
    plan = _plan(partition=["survival"], mods_dir=tmp_path / "shared_mods")

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated mods failure")

    monkeypatch.setattr(ss, "deploy_flat_files", boom)
    result = deploy_server_scope(cfg, plan, [], None)
    assert not result.success
    assert result.failure_phase == "mods"
    assert result.member_results == {}
    assert not (root / "config").exists()


def test_member_failure_halts_subsequent_members(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§4.2: a member failure stops the scope before the next member is touched."""
    _write_tree(tmp_path / "sync" / "config", {"c.toml": "x"})
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)
    cfg = _config(
        tmp_path,
        partition=["a", "b"],
        instances={"a": _instance("a", root_a), "b": _instance("b", root_b)},
    )
    plan = _plan(partition=["a", "b"])

    real_copy_tree = ss.copy_tree
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated copy_tree failure")
        return real_copy_tree(*args, **kwargs)

    monkeypatch.setattr(ss, "copy_tree", flaky)
    result = deploy_server_scope(cfg, plan, [], None)
    assert not result.success
    assert result.failure_member == "a"
    assert "b" not in result.member_results
    assert not (root_b / "config").exists()


# ---------------------------------------------------------------------------
# Result aggregation
# ---------------------------------------------------------------------------


def test_success_reports_all_members(tmp_path: Path) -> None:
    """A successful deploy reports a MemberWriteResult for every partition member."""
    _write_tree(tmp_path / "sync" / "config", {"c.toml": "x"})
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)
    cfg = _config(
        tmp_path,
        partition=["a", "b"],
        instances={"a": _instance("a", root_a), "b": _instance("b", root_b)},
    )
    result = deploy_server_scope(cfg, _plan(partition=["a", "b"]), [], None)
    assert result.success
    assert set(result.member_results.keys()) == {"a", "b"}
    assert (root_a / "config" / "c.toml").exists()
    assert (root_b / "config" / "c.toml").exists()


def test_result_aggregates_protected_kept() -> None:
    """ServerScopeResult.protected_kept aggregates across mods and members."""
    r = ServerScopeResult()
    r.mods_result = CopyResult(protected_kept=["a.jar"])
    m = MemberWriteResult(member="survival")
    m.config_result = CopyResult(protected_kept=["tokens.json"])
    r.member_results["survival"] = m
    assert set(r.protected_kept) == {"a.jar", "tokens.json"}


def test_result_aggregates_changed_count() -> None:
    """ServerScopeResult.changed_count aggregates across mods and members."""
    r = ServerScopeResult()
    r.mods_result = CopyResult(added=["a"], updated=["b"])
    m = MemberWriteResult(member="survival")
    m.config_result = CopyResult(added=["c"], removed=["d"])
    r.member_results["survival"] = m
    assert r.changed_count() == 4
