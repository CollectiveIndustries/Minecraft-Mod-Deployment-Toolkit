# tests/deploy_pack/test_preflight_slip.py

"""Slip-mode preflight tests (spec v1.3 Section 5.6, Section 5.8, Section 14.4, Section 14.7)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from minecraft.deploy_pack.config_model import (
    ComposeFile,
    ComposeLoadResult,
    ComposeService,
    DeploymentConfig,
    DockerConfig,
    InstanceConfig,
)
from minecraft.deploy_pack.preflight import runner
from minecraft.deploy_pack.preflight.changes import (
    compute_instance_server_change_from_context,
    compute_mods_change_from_context,
)
from minecraft.deploy_pack.preflight.types import PreflightError, PreflightFailure
from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.slip_context import (
    SlipContext,
    build_integration_context,
    build_release_context,
)

# ===========================================================================
# Test helpers
# ===========================================================================


def _hex(ch: str) -> str:
    """Return a 64-character hex string composed of the repeated character ``ch``."""
    return ch * 64


def _jar_entry(filename: str, content: bytes, side: str = "both") -> dict:
    """Build a valid [[jar]] entry whose sha256 matches ``content``."""
    return {
        "filename": filename,
        "sha256": hashlib.sha256(content).hexdigest(),
        "side": side,
        "provider": "modrinth",
        "pw_toml": f"{filename}.pw.toml",
        "pw_toml_sha256": _hex("0"),
        "dependencies": [],
    }


def _valid_slip_dict(jars: list[dict], release_mode: str = "update") -> dict:
    """Build a valid slip dict with a computed content_sha256."""
    body = {
        "packing_slip_version": 1,
        "release_id": "v1.0.0",
        "release_mode": release_mode,
        "meta": {
            "created_at_utc": "2026-10-12T00:00:00Z",
            "resolver_version": "minecraft-upgrade 1.3.0",
            "author": "test",
        },
        "runtime": {
            "minecraft_version": "1.20.1",
            "loader_type": "forge",
            "loader_version": "47.4.10",
            "image_reference": "itzg/minecraft-server:java25",
            "image_digest": "sha256:" + _hex("1"),
            "platform": "linux/amd64",
            "java_version": "21",
        },
        "inputs": {
            "git": {
                "config_tree": _hex("2"),
                "kubejs_tree": _hex("3"),
                "compose_tree": _hex("4"),
                "deploy_toml_tree": _hex("5"),
            },
        },
        "fingerprints": {
            "manifest_sha256": _hex("6"),
            "runtime_sha256": _hex("7"),
            "server_inputs_sha256": _hex("8"),
            "client_inputs_sha256": _hex("9"),
        },
        "jar": jars,
        "resource_pack": [],
        "velocity_plugin": [],
        "instances": {
            "survival": {
                "targeted": True,
                "required_sides": [],
                "config_mode": "merge",
                "kubejs_mode": "delete",
                "config_deletions": [],
                "protect_patterns": [],
            },
        },
    }
    body["content_sha256"] = slip_mod.compute_content_sha256(body)
    return body


def _build_ctx(
    tmp_path: Path,
    jar_contents: dict[str, bytes],
    mode: str = "release",
) -> SlipContext:
    """Materialize a candidate or bundle and return a verified SlipContext."""
    entries = [_jar_entry(name, content) for name, content in jar_contents.items()]
    parsed = slip_mod.parse_slip(_valid_slip_dict(entries))
    root = tmp_path / "bundle"
    (root / "artifacts").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "kubejs").mkdir()
    (root / "runtime").mkdir()
    for name, content in jar_contents.items():
        (root / "artifacts" / name).write_bytes(content)
    if mode == "release":
        return build_release_context(parsed, root)
    return build_integration_context(
        parsed,
        root,
        config_source=root / "config",
        kubejs_source=root / "kubejs",
        captured_head="deadbeef",
    )


def _make_service(
    name: str = "survival",
    image: str | None = None,
    platform: str | None = None,
) -> ComposeService:
    """Return a minimal ComposeService."""
    return ComposeService(
        name=name,
        container_name=f"mc-{name}",
        binds=[],
        stop_grace_period=None,
        stop_signal=None,
        has_healthcheck=True,
        secrets=[],
        environment={},
        env_files=[],
        image=image,
        platform=platform,
    )


def _make_compose(services: dict[str, ComposeService]) -> ComposeLoadResult:
    """Wrap services in a ComposeLoadResult."""
    return ComposeLoadResult(
        file=ComposeFile(
            path=Path("/dev/null"),
            base_dir=Path("/"),
            services=services,
            secret_files={},
        ),
        error=None,
    )


def _make_instance(
    name: str,
    service: ComposeService | None = None,
    instance_root: Path | None = None,
) -> InstanceConfig:
    """Return an InstanceConfig populated with the given service and root."""
    inst = InstanceConfig(
        name=name,
        container=f"mc-{name}",
        config_mode="merge",
        kubejs_mode="delete",
    )
    inst.service = service
    inst.instance_root = instance_root
    return inst


def _make_config(
    tmp_path: Path,
    instances: dict[str, InstanceConfig] | None = None,
    partition: list[str] | None = None,
    slip_ctx: SlipContext | None = None,
    compose_result: ComposeLoadResult | None = None,
) -> DeploymentConfig:
    """Build a minimal DeploymentConfig for slip-mode tests."""
    if instances is None:
        instances = {}
    if partition is None:
        partition = list(instances.keys())
    if compose_result is None:
        compose_result = ComposeLoadResult(file=None, error="test stub")
    return DeploymentConfig(
        project_root=tmp_path,
        config_dir=tmp_path / "config.d",
        sync_root=tmp_path / "sync",
        modpack_dir=tmp_path / "sync" / "downloads",
        www_dir=tmp_path / "www",
        www_dir_error=None,
        www_dir_candidates=[],
        output_filename="client.zip",
        download_base_url="https://example.com",
        protect_file=None,
        sync_mapping={},
        restart_policy={},
        instances=instances,
        partition=partition,
        partition_unknown=[],
        requested_instances=None,
        docker=DockerConfig(compose_file=tmp_path / "docker-compose.yml"),
        compose=compose_result,
        slip=slip_ctx,
    )


# ===========================================================================
# compute_mods_change_from_context
# ===========================================================================


def test_mods_change_from_context_all_buckets(tmp_path: Path) -> None:
    """Added, updated, and removed are computed from the slip jar set."""
    slip_ctx = _build_ctx(
        tmp_path,
        {"keep.jar": b"keep", "update.jar": b"new"},
    )
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    (mods_dir / "keep.jar").write_bytes(b"keep")
    (mods_dir / "update.jar").write_bytes(b"old")
    (mods_dir / "stale.jar").write_bytes(b"stale")

    result = compute_mods_change_from_context(slip_ctx, mods_dir)
    assert result is not None
    assert result.added == []
    assert result.updated == ["update.jar"]
    assert result.removed == ["stale.jar"]


def test_mods_change_from_context_added_only(tmp_path: Path) -> None:
    """A jar present only in the slip is reported as added."""
    slip_ctx = _build_ctx(tmp_path, {"new.jar": b"data"})
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()

    result = compute_mods_change_from_context(slip_ctx, mods_dir)
    assert result is not None
    assert result.added == ["new.jar"]
    assert result.updated == []
    assert result.removed == []


def test_mods_change_from_context_none_mods_dir(tmp_path: Path) -> None:
    """mods_dir=None returns None."""
    slip_ctx = _build_ctx(tmp_path, {})
    assert compute_mods_change_from_context(slip_ctx, None) is None


# ===========================================================================
# compute_instance_server_change_from_context
# ===========================================================================


def test_instance_server_change_from_context(tmp_path: Path) -> None:
    """Config and kubejs trees are diffed from the context sources."""
    slip_ctx = _build_ctx(tmp_path, {})
    (slip_ctx.config_source / "server.properties").write_bytes(b"motd=new")
    (slip_ctx.kubejs_source / "script.js").write_bytes(b"line1")
    inst_root = tmp_path / "instance"
    (inst_root / "config").mkdir(parents=True)
    (inst_root / "kubejs").mkdir()
    (inst_root / "config" / "server.properties").write_bytes(b"motd=old")
    (inst_root / "kubejs" / "old.js").write_bytes(b"old")
    inst = _make_instance("survival", instance_root=inst_root)
    config = _make_config(tmp_path, instances={"survival": inst}, slip_ctx=slip_ctx)

    result = compute_instance_server_change_from_context(config, "survival", slip_ctx)
    assert result.member == "survival"
    assert any("server.properties" in p for p in result.updated)
    assert any("script.js" in p for p in result.added)
    assert any("old.js" in p for p in result.removed)


# ===========================================================================
# _check_slip_runtime
# ===========================================================================


def test_check_slip_runtime_matches(tmp_path: Path) -> None:
    """A matching image and platform produce no failures, but add the warning."""
    slip_ctx = _build_ctx(tmp_path, {})
    svc = _make_service(image="itzg/minecraft-server:java25", platform="linux/amd64")
    inst = _make_instance("survival", service=svc, instance_root=tmp_path)
    config = _make_config(
        tmp_path,
        instances={"survival": inst},
        compose_result=_make_compose({"survival": svc}),
        slip_ctx=slip_ctx,
    )
    failures: list[PreflightFailure] = []
    warnings: list[str] = []
    runner._check_slip_runtime(failures, warnings, config, runner._log)
    assert failures == []
    assert len(warnings) == 1
    assert "not yet reconciled" in warnings[0]


def test_check_slip_runtime_image_mismatch(tmp_path: Path) -> None:
    """A different image reference fails with Section 5.6 source."""
    slip_ctx = _build_ctx(tmp_path, {})
    svc = _make_service(image="itzg/minecraft-server:java17", platform="linux/amd64")
    inst = _make_instance("survival", service=svc, instance_root=tmp_path)
    config = _make_config(
        tmp_path,
        instances={"survival": inst},
        compose_result=_make_compose({"survival": svc}),
        slip_ctx=slip_ctx,
    )
    failures: list[PreflightFailure] = []
    warnings: list[str] = []
    runner._check_slip_runtime(failures, warnings, config, runner._log)
    assert len(failures) == 1
    assert failures[0].source == "Section 5.6 [survival]"
    assert "image_reference mismatch" in failures[0].message


def test_check_slip_runtime_platform_mismatch(tmp_path: Path) -> None:
    """A different platform fails with Section 5.6 source."""
    slip_ctx = _build_ctx(tmp_path, {})
    svc = _make_service(image="itzg/minecraft-server:java25", platform="linux/arm64")
    inst = _make_instance("survival", service=svc, instance_root=tmp_path)
    config = _make_config(
        tmp_path,
        instances={"survival": inst},
        compose_result=_make_compose({"survival": svc}),
        slip_ctx=slip_ctx,
    )
    failures: list[PreflightFailure] = []
    warnings: list[str] = []
    runner._check_slip_runtime(failures, warnings, config, runner._log)
    assert len(failures) == 1
    assert "platform mismatch" in failures[0].message


def test_check_slip_runtime_skips_when_service_absent(tmp_path: Path) -> None:
    """An instance with no matching service is silently skipped."""
    slip_ctx = _build_ctx(tmp_path, {})
    inst = _make_instance("survival", service=None, instance_root=tmp_path)
    config = _make_config(
        tmp_path,
        instances={"survival": inst},
        slip_ctx=slip_ctx,
    )
    failures: list[PreflightFailure] = []
    warnings: list[str] = []
    runner._check_slip_runtime(failures, warnings, config, runner._log)
    assert failures == []


# ===========================================================================
# _check_slip_working_tree
# ===========================================================================


def test_check_slip_working_tree_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean working tree produces no failures."""

    def fake_git(_args: list[str], _cwd: Path) -> tuple[int, str]:
        """Returns a clean git status."""
        return (0, "")

    monkeypatch.setattr(runner, "_run_git", fake_git)
    config = _make_config(tmp_path)
    failures: list[PreflightFailure] = []
    runner._check_slip_working_tree(failures, config, runner._log)
    assert failures == []


def test_check_slip_working_tree_dirty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dirty working tree fails with Section 14.7 source."""

    def fake_git(_args: list[str], _cwd: Path) -> tuple[int, str]:
        """Returns a non-empty git status."""
        return (0, "M sync/config/server.properties")

    monkeypatch.setattr(runner, "_run_git", fake_git)
    config = _make_config(tmp_path)
    failures: list[PreflightFailure] = []
    runner._check_slip_working_tree(failures, config, runner._log)
    assert len(failures) == 1
    assert failures[0].source == "Section 14.7"
    assert "working tree is dirty" in failures[0].message


def test_check_slip_working_tree_git_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When git is unavailable, fail with Section 14.7."""

    def fake_git(_args: list[str], _cwd: Path) -> tuple[int, str]:
        """Simulates a missing git binary."""
        return (127, "")

    monkeypatch.setattr(runner, "_run_git", fake_git)
    config = _make_config(tmp_path)
    failures: list[PreflightFailure] = []
    runner._check_slip_working_tree(failures, config, runner._log)
    assert len(failures) == 1
    assert failures[0].source == "Section 14.7"
    assert "git is not available" in failures[0].message


# ===========================================================================
# _check_slip_tree_hashes
# ===========================================================================


def test_check_slip_tree_hashes_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When all tree hashes match, no failures."""
    slip_ctx = _build_ctx(tmp_path, {})
    inputs = slip_ctx.slip.inputs_git

    expected = {
        "sync/config": inputs.config_tree,
        "sync/kubejs": inputs.kubejs_tree,
        "docker-compose.yml": inputs.compose_tree,
        "config.d/deploy_pack.toml": inputs.deploy_toml_tree,
    }

    def fake_git(args: list[str], _cwd: Path) -> tuple[int, str]:
        """Returns the expected hash for each HEAD:path query."""
        target = args[-1]
        relpath = target.split("HEAD:", 1)[1]
        return (0, expected[relpath])

    monkeypatch.setattr(runner, "_run_git", fake_git)
    config = _make_config(tmp_path, slip_ctx=slip_ctx)
    failures: list[PreflightFailure] = []
    runner._check_slip_tree_hashes(failures, config, runner._log)
    assert failures == []


def test_check_slip_tree_hashes_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mismatch fails with Section 5.6 source."""
    slip_ctx = _build_ctx(tmp_path, {})

    def fake_git(_args: list[str], _cwd: Path) -> tuple[int, str]:
        """Returns a different hash for every query."""
        return (0, _hex("f"))

    monkeypatch.setattr(runner, "_run_git", fake_git)
    config = _make_config(tmp_path, slip_ctx=slip_ctx)
    failures: list[PreflightFailure] = []
    runner._check_slip_tree_hashes(failures, config, runner._log)
    assert len(failures) == 4
    assert all(f.source == "Section 5.6" for f in failures)
    assert all("mismatch" in f.message for f in failures)


def test_check_slip_tree_hashes_git_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero git return code fails each check."""
    slip_ctx = _build_ctx(tmp_path, {})

    def fake_git(_args: list[str], _cwd: Path) -> tuple[int, str]:
        """Simulates a git failure."""
        return (128, "")

    monkeypatch.setattr(runner, "_run_git", fake_git)
    config = _make_config(tmp_path, slip_ctx=slip_ctx)
    failures: list[PreflightFailure] = []
    runner._check_slip_tree_hashes(failures, config, runner._log)
    assert len(failures) == 4
    assert all("cannot verify" in f.message for f in failures)


# ===========================================================================
# _check_slip_compatibility dispatch
# ===========================================================================


def test_check_slip_compatibility_no_slip(tmp_path: Path) -> None:
    """Without a slip, the composite check is a no-op."""
    config = _make_config(tmp_path)
    failures: list[PreflightFailure] = []
    warnings: list[str] = []
    runner._check_slip_compatibility(failures, warnings, config, runner._log)
    assert failures == []
    assert warnings == []


def test_check_slip_compatibility_integration_skips_tree_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration mode does not call git status or git rev-parse."""
    slip_ctx = _build_ctx(tmp_path, {}, mode="integration")
    calls: list[list[str]] = []

    def fake_git(args: list[str], _cwd: Path) -> tuple[int, str]:
        """Records calls and returns empty."""
        calls.append(args)
        return (0, "")

    monkeypatch.setattr(runner, "_run_git", fake_git)
    svc = _make_service(image="itzg/minecraft-server:java25", platform="linux/amd64")
    inst = _make_instance("survival", service=svc, instance_root=tmp_path)
    config = _make_config(
        tmp_path,
        instances={"survival": inst},
        compose_result=_make_compose({"survival": svc}),
        slip_ctx=slip_ctx,
    )
    failures: list[PreflightFailure] = []
    warnings: list[str] = []
    runner._check_slip_compatibility(failures, warnings, config, runner._log)
    assert failures == []
    assert calls == []


# ===========================================================================
# _raise_if_slip_failures
# ===========================================================================


def test_raise_if_slip_failures_bails() -> None:
    """A Section 5.6 failure triggers the early bail."""
    failures = [PreflightFailure("Section 5.6", "bad")]
    with pytest.raises(PreflightError):
        runner._raise_if_slip_failures(failures, runner._log)


def test_raise_if_slip_failures_does_not_bail_on_other_sources() -> None:
    """A non-slip failure does not trigger the slip bail."""
    failures = [PreflightFailure("Section 3.5", "compose broken")]
    runner._raise_if_slip_failures(failures, runner._log)
