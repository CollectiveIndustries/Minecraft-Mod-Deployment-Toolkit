# tests/deploy_pack/test_scope_server.py

"""Tests for deploy_pack.scope_server, per Project_Specs.md v3.0 §10.1.

Coverage areas:
  * mods deployment: source set from index, side filter, closure
  * mods_dir skipped when targeted (§2.9)
  * mods_dir skipped when plan.mods_dir is None
  * config: merge mode (extras kept)
  * config: delete mode (extras removed)
  * kubejs: always delete mode
  * other sync-mapping keys: merge default
  * shared dest (@www/...) skipped
  * protect patterns apply to mods and to config/kubejs
  * halt on first failure; later members untouched
  * result reports per-member and mods results
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.deploy_pack import scope_server as ss
from minecraft.deploy_pack.config_model import DeploymentConfig, DiscordConfig, DockerConfig, InstanceConfig
from minecraft.deploy_pack.files import CopyResult
from minecraft.deploy_pack.preflight import PreflightPlan, ScopeSet
from minecraft.deploy_pack.scope_server import MemberWriteResult, ServerScopeResult, _mode_for_key, _resolve_mods_source, deploy_server_scope


def _config(tmp_path: Path, *, partition: list[str], instances: dict[str, InstanceConfig], sync_mapping: dict | None = None) -> DeploymentConfig:
    return DeploymentConfig(
        project_root=tmp_path,
        config_dir=tmp_path / "config.d",
        sync_root=tmp_path / "sync",
        modpack_dir=tmp_path / "sync" / "downloads",
        www_dir=tmp_path / "www",
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
        compose=None,
        mods_dir_toml=None,
    )


def _instance(name: str, root: Path, *, config_mode: str = "merge", kubejs_mode: str = "delete") -> InstanceConfig:
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


def _plan(*, partition: list[str], mods_dir: Path | None = None, targeted: bool = False) -> PreflightPlan:
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
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _read_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_text(encoding="utf-8")
    return out


def test_mode_for_config_uses_instance_setting(tmp_path: Path) -> None:
    """Tests that config mode resolution uses the instance-specific setting."""
    inst = _instance("a", tmp_path / "a", config_mode="merge")
    assert _mode_for_key("config", inst) == "merge"
    inst2 = _instance("b", tmp_path / "b", config_mode="delete")
    assert _mode_for_key("config", inst2) == "delete"


def test_mode_for_kubejs_is_delete(tmp_path: Path) -> None:
    """Tests that the mode for the kubejs key is delete."""
    inst = _instance("a", tmp_path / "a")
    assert _mode_for_key("kubejs", inst) == "delete"


def test_mode_for_unknown_key_is_merge(tmp_path: Path) -> None:
    """Tests that the mode for an unknown key defaults to merge."""
    inst = _instance("a", tmp_path / "a")
    assert _mode_for_key("scripts", inst) == "merge"


def test_resolve_mods_source_empty_when_no_index(tmp_path: Path) -> None:
    """Tests that resolving the mods source returns an empty mapping when no index is provided."""
    cfg = _config(tmp_path, partition=[], instances={})
    assert _resolve_mods_source(cfg, None) == {}


def test_resolve_mods_source_side_filter(tmp_path: Path) -> None:
    """Tests that resolve_mods_source filters mods by server-side when partition is empty."""
    index = tmp_path / "sync" / "downloads" / ".index"
    index.mkdir(parents=True)
    (index / "a.pw.toml").write_text('filename = "a.jar"\nside = "server"\n', encoding="utf-8")
    (index / "b.pw.toml").write_text('filename = "b.jar"\nside = "client"\n', encoding="utf-8")
    downloads = tmp_path / "sync" / "downloads"
    (downloads / "a.jar").write_bytes(b"x")
    (downloads / "b.jar").write_bytes(b"y")
    cfg = _config(tmp_path, partition=[], instances={})
    src = _resolve_mods_source(cfg, None)
    assert set(src.keys()) == {"a.jar"}


def test_resolve_mods_source_missing_file_skipped(tmp_path: Path) -> None:
    """Tests that resolve_mods_source skips index entries whose files are missing on disk."""
    index = tmp_path / "sync" / "downloads" / ".index"
    index.mkdir(parents=True)
    (index / "a.pw.toml").write_text('filename = "a.jar"\nside = "server"\n', encoding="utf-8")
    cfg = _config(tmp_path, partition=[], instances={})
    src = _resolve_mods_source(cfg, None)
    assert src == {}


def test_resolve_mods_source_applies_overrides(tmp_path: Path) -> None:
    """Tests that resolve_mods_source applies side overrides from config.d by filename."""
    index = tmp_path / "sync" / "downloads" / ".index"
    index.mkdir(parents=True)
    (index / "a.pw.toml").write_text('filename = "a.jar"\nside = "client"\n', encoding="utf-8")
    downloads = tmp_path / "sync" / "downloads"
    (downloads / "a.jar").write_bytes(b"x")
    cfg_dir = tmp_path / "config.d"
    cfg_dir.mkdir()
    (cfg_dir / "side_overrides.toml").write_text('[by_filename]\n"a.jar" = "both"\n', encoding="utf-8")
    cfg = _config(tmp_path, partition=[], instances={})
    src = _resolve_mods_source(cfg, None)
    assert set(src.keys()) == {"a.jar"}


def test_mods_deployed(tmp_path: Path) -> None:
    """Tests that deploy_server_scope deploys server-side mods to the mods directory."""
    index = tmp_path / "sync" / "downloads" / ".index"
    index.mkdir(parents=True)
    (index / "a.pw.toml").write_text('filename = "a.jar"\nside = "server"\n', encoding="utf-8")
    downloads = tmp_path / "sync" / "downloads"
    (downloads / "a.jar").write_bytes(b"content")
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir()
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": _instance("survival", tmp_path / "survival")})
    plan = _plan(partition=["survival"], mods_dir=mods_dir)
    result = deploy_server_scope(cfg, plan, [], None)
    assert result.success
    assert result.mods_result is not None
    assert result.mods_result.added == ["a.jar"]
    assert (mods_dir / "a.jar").read_bytes() == b"content"


def test_mods_removes_stale(tmp_path: Path) -> None:
    """Tests that deploy_server_scope removes stale mods no longer present in the source."""
    index = tmp_path / "sync" / "downloads" / ".index"
    index.mkdir(parents=True)
    (index / "a.pw.toml").write_text('filename = "a.jar"\nside = "server"\n', encoding="utf-8")
    downloads = tmp_path / "sync" / "downloads"
    (downloads / "a.jar").write_bytes(b"x")
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir()
    (mods_dir / "stale.jar").write_bytes(b"y")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": _instance("survival", tmp_path / "survival")})
    plan = _plan(partition=["survival"], mods_dir=mods_dir)
    result = deploy_server_scope(cfg, plan, [], None)
    assert result.mods_result.removed == ["stale.jar"]
    assert not (mods_dir / "stale.jar").exists()


def test_mods_protect_keeps_stale(tmp_path: Path) -> None:
    """Tests that protected stale mods are kept in the mods directory."""
    index = tmp_path / "sync" / "downloads" / ".index"
    index.mkdir(parents=True)
    (index / "a.pw.toml").write_text('filename = "a.jar"\nside = "server"\n', encoding="utf-8")
    downloads = tmp_path / "sync" / "downloads"
    (downloads / "a.jar").write_bytes(b"x")
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir()
    (mods_dir / "keep.jar").write_bytes(b"y")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": _instance("survival", tmp_path / "survival")})
    plan = _plan(partition=["survival"], mods_dir=mods_dir)
    result = deploy_server_scope(cfg, plan, ["keep.jar"], None)
    assert (mods_dir / "keep.jar").exists()
    assert "keep.jar" in result.protected_kept


def test_mods_skipped_when_targeted(tmp_path: Path) -> None:
    """Tests that mod deployment is skipped when the plan targets specific files."""
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir()
    (mods_dir / "existing.jar").write_bytes(b"x")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": _instance("survival", tmp_path / "survival")})
    plan = _plan(partition=["survival"], mods_dir=mods_dir, targeted=True)
    result = deploy_server_scope(cfg, plan, [], None)
    assert result.mods_skipped_reason is not None
    assert result.mods_result is None
    assert (mods_dir / "existing.jar").exists()


def test_mods_skipped_when_plan_mods_dir_none(tmp_path: Path) -> None:
    """Tests that mod deployment is skipped when the plan's mods directory is None."""
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": _instance("survival", tmp_path / "survival")})
    plan = _plan(partition=["survival"], mods_dir=None)
    result = deploy_server_scope(cfg, plan, [], None)
    assert result.mods_skipped_reason is not None
    assert result.mods_result is None


def test_config_merge_keeps_extras(tmp_path: Path) -> None:
    """Tests that merging configs preserves extra files while updating common ones."""
    sync = tmp_path / "sync"
    _write_tree(sync / "config", {"common.toml": "new"})
    root = tmp_path / "survival"
    _write_tree(root / "config", {"common.toml": "new", "extra.toml": "keep"})
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": _instance("survival", root, config_mode="merge")})
    plan = _plan(partition=["survival"])
    result = deploy_server_scope(cfg, plan, [], None)
    assert result.success
    assert (root / "config" / "extra.toml").exists()
    assert (root / "config" / "common.toml").read_text() == "new"


def test_config_delete_removes_extras(tmp_path: Path) -> None:
    """Tests that config deletion removes extra files."""
    sync = tmp_path / "sync"
    _write_tree(sync / "config", {"common.toml": "x"})
    root = tmp_path / "survival"
    _write_tree(root / "config", {"common.toml": "x", "extra.toml": "gone"})
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": _instance("survival", root, config_mode="delete")})
    plan = _plan(partition=["survival"])
    result = deploy_server_scope(cfg, plan, [], None)
    assert not (root / "config" / "extra.toml").exists()
    member = result.member_results["survival"]
    assert member.config_result is not None
    assert "extra.toml" in member.config_result.removed


def test_kubejs_delete_mode_regardless_of_instance_setting(tmp_path: Path) -> None:
    """§7.2: kubejs_mode is always delete."""
    sync = tmp_path / "sync"
    _write_tree(sync / "kubejs", {"scripts/a.js": "new"})
    root = tmp_path / "survival"
    _write_tree(root / "kubejs", {"scripts/a.js": "new", "stale.js": "x"})
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": _instance("survival", root, kubejs_mode="delete")})
    plan = _plan(partition=["survival"])
    deploy_server_scope(cfg, plan, [], None)
    assert not (root / "kubejs" / "stale.js").exists()


def test_other_sync_mapping_key_defaults_to_merge(tmp_path: Path) -> None:
    """Tests that a sync mapping key defaults to merge behavior."""
    sync = tmp_path / "sync"
    _write_tree(sync / "extra", {"a.txt": "1"})
    root = tmp_path / "survival"
    _write_tree(root / "extra", {"a.txt": "1", "keep.txt": "x"})
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", root)},
        sync_mapping={"config": "config", "kubejs": "kubejs", "extra": "extra"},
    )
    plan = _plan(partition=["survival"])
    result = deploy_server_scope(cfg, plan, [], None)
    assert (root / "extra" / "keep.txt").exists()
    member = result.member_results["survival"]
    assert "extra" in member.other_results


def test_shared_dest_skipped(tmp_path: Path) -> None:
    """Tests that a shared destination is skipped during deployment."""
    sync = tmp_path / "sync"
    _write_tree(sync / "resourcepacks", {"pack.zip": "x"})
    root = tmp_path / "survival"
    root.mkdir(parents=True, exist_ok=True)
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": _instance("survival", root)},
        sync_mapping={"config": "config", "kubejs": "kubejs", "resourcepacks": {"server": "@www/resourcepacks", "client": "resourcepacks"}},
    )
    plan = _plan(partition=["survival"])
    deploy_server_scope(cfg, plan, [], None)
    assert not (root / "resourcepacks").exists()


def test_missing_source_dir_is_silent(tmp_path: Path) -> None:
    """A sync-mapping key whose source dir is absent is skipped."""
    root = tmp_path / "survival"
    root.mkdir(parents=True)
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": _instance("survival", root)})
    plan = _plan(partition=["survival"])
    result = deploy_server_scope(cfg, plan, [], None)
    assert result.success
    assert result.member_results["survival"].config_result is None


def test_halt_on_mods_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mods failure halts before any member is touched."""
    index = tmp_path / "sync" / "downloads" / ".index"
    index.mkdir(parents=True)
    (index / "a.pw.toml").write_text('filename = "a.jar"\nside = "server"\n', encoding="utf-8")
    (tmp_path / "sync" / "downloads" / "a.jar").write_bytes(b"x")
    sync = tmp_path / "sync"
    _write_tree(sync / "config", {"c.toml": "x"})
    root = tmp_path / "survival"
    root.mkdir(parents=True)
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": _instance("survival", root)})
    plan = _plan(partition=["survival"], mods_dir=tmp_path / "shared_mods")

    def boom(*a, **kw):
        raise OSError("simulated mods failure")

    monkeypatch.setattr(ss, "deploy_flat_files", boom)
    result = deploy_server_scope(cfg, plan, [], None)
    assert not result.success
    assert result.failure_phase == "mods"
    assert result.member_results == {}
    assert not (root / "config").exists()


def test_halt_on_member_failure_stops_subsequent_members(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that a member failure halts deployment of subsequent members."""
    sync = tmp_path / "sync"
    _write_tree(sync / "config", {"c.toml": "x"})
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)
    cfg = _config(tmp_path, partition=["a", "b"], instances={"a": _instance("a", root_a), "b": _instance("b", root_b)})
    plan = _plan(partition=["a", "b"])
    real_copy_tree = ss.copy_tree
    call_count = {"n": 0}

    def flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated copy_tree failure")
        return real_copy_tree(*args, **kwargs)

    monkeypatch.setattr(ss, "copy_tree", flaky)
    result = deploy_server_scope(cfg, plan, [], None)
    assert not result.success
    assert result.failure_member == "a"
    assert "b" not in result.member_results
    assert not (root_b / "config").exists()


def test_success_reports_all_members(tmp_path: Path) -> None:
    """Verifies deployment succeeds and reports results for all partition members."""
    sync = tmp_path / "sync"
    _write_tree(sync / "config", {"c.toml": "x"})
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)
    cfg = _config(tmp_path, partition=["a", "b"], instances={"a": _instance("a", root_a), "b": _instance("b", root_b)})
    plan = _plan(partition=["a", "b"])
    result = deploy_server_scope(cfg, plan, [], None)
    assert result.success
    assert set(result.member_results.keys()) == {"a", "b"}
    assert (root_a / "config" / "c.toml").exists()
    assert (root_b / "config" / "c.toml").exists()


def test_result_protected_kept_aggregates() -> None:
    """Tests that protected_kept aggregates protected files from mods and member configs."""
    r = ServerScopeResult()
    r.mods_result = CopyResult(protected_kept=["a.jar"])
    m = MemberWriteResult(member="survival")
    m.config_result = CopyResult(protected_kept=["tokens.json"])
    r.member_results["survival"] = m
    assert set(r.protected_kept) == {"a.jar", "tokens.json"}


def test_result_changed_count_aggregates() -> None:
    """Tests that changed_count aggregates added, updated, and removed entries across mods and members."""
    r = ServerScopeResult()
    r.mods_result = CopyResult(added=["a"], updated=["b"])
    m = MemberWriteResult(member="survival")
    m.config_result = CopyResult(added=["c"], removed=["d"])
    r.member_results["survival"] = m
    assert r.changed_count() == 4
