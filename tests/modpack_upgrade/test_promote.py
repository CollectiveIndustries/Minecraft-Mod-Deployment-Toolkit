# tests/modpack_upgrade/test_promote.py

"""Tests for ``minecraft-upgrade promote`` (Section 15.1.7).

Update-mode promote is a regression check (unchanged behavior: copy
slip + metadata, ``git add``, print instructions).

Upgrade-mode promote additionally reconciles ``docker-compose.yml``
against the slip's ``[runtime]``, refreshes the slip's
``[inputs.git].compose_tree`` to reflect the working-tree blob hash,
and re-seals with a fresh ``content_sha256``. The four possible
outcomes are covered:

  * compose matches + tree matches -> no-op reconciliation, slip
    copied as-is;
  * compose matches + tree drifted -> re-seal only, compose file
    untouched;
  * compose differs + tree drifted -> reconcile compose, re-seal,
    compose file becomes part of ``git add``;
  * compose missing or ambiguous -> exit 3 (preconditions unchanged).

The ``_setup_promote_env`` fixture defaults the compose file to match
the slip's runtime (no drift). Tests that exercise reconciliation
diffing must pass an explicit drifted ``compose_body``; the fixture is
deliberately conservative so most tests read cleanly.

``_git_add`` and ``_git_blob_hash`` are monkeypatched at the module
level for the behavior tests, so those tests focus on orchestration,
not on git mechanics. Slice 9 Turn C1 additionally adds integration
tests (bottom of file) that call the real ``_git_add`` and
``_git_blob_hash`` against a real ``tmp_path`` git repository. Those
tests close the ~10% coverage gap on the two function bodies after the
migration to ``PlatformCore.git``.

Every test that wires ``_wire_git`` captures its return value, even
when only one half is used. For pre-condition failure tests, the
``added == []`` assertion pins that promote aborted before staging
anything - a stronger check than merely "raised exit 3."
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest
import tomli_w
import yaml

from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.cli.commands import promote
from minecraft.modpack_upgrade.cli.commands.promote import run
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.state import (
    STATUS_MATERIALIZED,
    RunState,
    save_state,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _build_slip_raw(
    *,
    run_id: str = "run-1",
    release_mode: str = "upgrade",
    mc_version: str = "1.21.1",
    loader_type: str = "forge",
    loader_version: str = "52.0.1",
    compose_tree: str = "abc123",
) -> dict:
    """Build a valid slip dict with a fresh ``content_sha256``."""
    raw = {
        "packing_slip_version": 1,
        "release_id": run_id,
        "release_mode": release_mode,
        "meta": {
            "created_at_utc": "2026-01-01T00:00:00Z",
            "resolver_version": "minecraft-upgrade 1.3.1",
            "author": "test",
        },
        "runtime": {
            "minecraft_version": mc_version,
            "loader_type": loader_type,
            "loader_version": loader_version,
            "image_reference": "itzg/minecraft-server:java25",
            "image_digest": "sha256:" + _hex("1"),
            "platform": "linux/amd64",
            "java_version": "21",
        },
        "inputs": {
            "git": {
                "config_tree": _hex("2"),
                "kubejs_tree": _hex("3"),
                "compose_tree": compose_tree,
                "deploy_toml_tree": _hex("5"),
            },
        },
        "fingerprints": {
            "manifest_sha256": _hex("6"),
            "resolution_inputs_sha256": _hex("7"),
            "runtime_sha256": _hex("8"),
            "server_inputs_sha256": _hex("9"),
            "client_inputs_sha256": _hex("a"),
        },
        "jar": [],
        "resource_pack": [],
        "velocity_plugin": [],
        "instances": {
            "survival": {
                "targeted": True,
                "required_sides": ["both", "server"],
                "config_mode": "merge",
                "kubejs_mode": "delete",
                "config_deletions": [],
                "protect_patterns": [],
            },
        },
    }
    raw["content_sha256"] = slip_mod.compute_content_sha256(raw)
    return raw


def _compose_body(
    *,
    mc_version: str = "1.21.1",
    loader: str = "FORGE",
    loader_version: str = "52.0.1",
) -> str:
    """Build a minimal compose file using the itzg env-var convention."""
    return (
        "services:\n"
        "  mc-survival:\n"
        "    image: itzg/minecraft-server\n"
        "    environment:\n"
        f'      VERSION: "{mc_version}"\n'
        f'      TYPE: "{loader}"\n'
        f'      FORGE_VERSION: "{loader_version}"\n'
    )


def _setup_promote_env(
    tmp_path: Path,
    *,
    release_mode: str = "upgrade",
    slip_mc_version: str = "1.21.1",
    slip_loader: str = "forge",
    slip_loader_version: str = "52.0.1",
    slip_compose_tree: str = "abc123",
    compose_body: str | None = None,
    write_compose: bool = True,
) -> tuple[Path, Path, str]:
    """Set up a project tree with one candidate ready for promote.

    When ``compose_body`` is None, the default compose file matches the
    slip's runtime (no drift). Callers exercising reconciliation diffing
    must pass an explicit drifted body.

    When ``release_mode == "update"``, the state includes a placeholder
    ``effective_compose_sha256``. ``state.load_state`` requires this field
    for update mode (Resolution_Report Section 6.1); promote itself does
    not read it, so any non-None value suffices.

    Returns ``(config_dir, project_root, run_id)``.
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    state_dir = tmp_path / "state"
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{state_dir}"\n',
        encoding="utf-8",
    )

    run_id = "run-1"
    candidate_dir = state_dir / "candidates" / run_id
    candidate_dir.mkdir(parents=True)
    slip_raw = _build_slip_raw(
        run_id=run_id,
        release_mode=release_mode,
        mc_version=slip_mc_version,
        loader_type=slip_loader,
        loader_version=slip_loader_version,
        compose_tree=slip_compose_tree,
    )
    (candidate_dir / "packing_slip.toml").write_text(tomli_w.dumps(slip_raw), encoding="utf-8")

    metadata_dir = candidate_dir / "metadata"
    metadata_dir.mkdir()
    (metadata_dir / "mod.pw.toml").write_text('filename = "mod.jar"\nside = "both"\n')

    run_state_dir = state_dir / "runs" / run_id
    run_state_dir.mkdir(parents=True)
    state = RunState(
        run_id=run_id,
        status=STATUS_MATERIALIZED,
        release_mode=release_mode,
        paused_at_utc="2026-01-01T00:00:00Z",
        report_sha256=_hex("a"),
        resolution_inputs_sha256=_hex("b"),
        manifest_path=str(tmp_path / "sync" / "modpack.manifest.toml"),
        effective_compose_sha256=(_hex("c") if release_mode == "update" else None),
    )
    save_state(run_state_dir / "state.toml", state)

    if write_compose:
        (tmp_path / "docker-compose.yml").write_text(
            compose_body
            if compose_body is not None
            else _compose_body(
                mc_version=slip_mc_version,
                loader=slip_loader.upper(),
                loader_version=slip_loader_version,
            ),
            encoding="utf-8",
        )

    (tmp_path / "sync" / "downloads" / ".index").mkdir(parents=True)
    (tmp_path / "sync" / "packing_slips").mkdir(parents=True)

    return config_dir, tmp_path, run_id


@pytest.fixture
def _wire_git(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Path], dict[str, str]]:
    """Monkeypatch ``_git_add`` and ``_git_blob_hash``.

    Returns ``(added, blob_state)``. ``added`` accumulates the paths
    passed to ``_git_add``; ``blob_state["value"]`` is the string that
    ``_git_blob_hash`` returns (mutable so each test can set it).
    """
    added: list[Path] = []
    blob_state: dict[str, str] = {"value": "default_tree_hash"}

    def _fake_add(project_root: Path, paths: list[Path]) -> None:
        added.extend(paths)

    def _fake_blob_hash(project_root: Path, relpath: str) -> str:
        return blob_state["value"]

    monkeypatch.setattr(promote, "_git_add", _fake_add)
    monkeypatch.setattr(promote, "_git_blob_hash", _fake_blob_hash)

    return added, blob_state


# ---------------------------------------------------------------------------
# Update-mode regression (unchanged behavior)
# ---------------------------------------------------------------------------


def test_update_mode_copies_slip_and_metadata(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """Update-mode promote: no reconciliation; only slip + metadata staged."""
    added, _ = _wire_git
    config_dir, project_root, _ = _setup_promote_env(tmp_path, release_mode="update")

    code = run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")

    assert code == 0
    assert (project_root / "sync" / "packing_slips" / "v1.0.0.toml").is_file()
    assert (project_root / "sync" / "downloads" / ".index" / "mod.pw.toml").is_file()

    added_rel = {str(p.relative_to(project_root)) for p in added}
    assert "sync/packing_slips/v1.0.0.toml" in added_rel
    assert "sync/downloads/.index/mod.pw.toml" in added_rel
    assert "docker-compose.yml" not in added_rel


def test_update_mode_does_not_touch_compose(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """Update mode leaves compose bytes untouched regardless of runtime drift.

    Also pins that compose is NOT staged: even though the slip runtime
    (1.21.2) disagrees with compose (1.21.1), update mode must not
    reconcile or stage the compose file.
    """
    added, _ = _wire_git
    compose_before = _compose_body(mc_version="1.21.1")
    config_dir, project_root, _ = _setup_promote_env(
        tmp_path,
        release_mode="update",
        slip_mc_version="1.21.2",
        compose_body=compose_before,
    )

    run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")

    compose_after = (project_root / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose_after == compose_before

    added_rel = {str(p.relative_to(project_root)) for p in added}
    assert "docker-compose.yml" not in added_rel


# ---------------------------------------------------------------------------
# Upgrade mode: reconciliation
# ---------------------------------------------------------------------------


def test_upgrade_mode_no_changes_no_reseal(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """Compose matches runtime and tree already current -> no-op reconciliation."""
    added, blob_state = _wire_git
    blob_state["value"] = "current_tree_hash"
    config_dir, project_root, _ = _setup_promote_env(
        tmp_path,
        release_mode="upgrade",
        slip_compose_tree="current_tree_hash",
    )

    code = run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")

    assert code == 0
    copied = tomllib.loads((project_root / "sync" / "packing_slips" / "v1.0.0.toml").read_text())
    assert copied["inputs"]["git"]["compose_tree"] == "current_tree_hash"
    added_rel = {str(p.relative_to(project_root)) for p in added}
    assert "docker-compose.yml" not in added_rel


def test_upgrade_mode_reconciles_compose_and_reseals_slip(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """Compose drifted from slip runtime -> reconciled; slip re-sealed; compose staged.

    The fixture's default compose matches the slip runtime, so this test
    passes an explicit drifted ``compose_body`` (mc=1.21.1) while the
    slip targets 1.21.2. Reconciliation must update VERSION to 1.21.2,
    re-seal the slip with the new tree hash, and stage docker-compose.yml.
    """
    added, blob_state = _wire_git
    blob_state["value"] = "post_reconcile_tree_hash"
    config_dir, project_root, _ = _setup_promote_env(
        tmp_path,
        release_mode="upgrade",
        slip_mc_version="1.21.2",
        slip_compose_tree="pre_reconcile_tree_hash",
        compose_body=_compose_body(mc_version="1.21.1"),
    )

    code = run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")

    assert code == 0

    on_disk = yaml.safe_load((project_root / "docker-compose.yml").read_bytes())
    env = on_disk["services"]["mc-survival"]["environment"]
    assert env["VERSION"] == "1.21.2"

    copied = tomllib.loads((project_root / "sync" / "packing_slips" / "v1.0.0.toml").read_text())
    assert copied["inputs"]["git"]["compose_tree"] == "post_reconcile_tree_hash"

    added_rel = {str(p.relative_to(project_root)) for p in added}
    assert "docker-compose.yml" in added_rel


def test_upgrade_mode_reseals_when_tree_drifted_but_runtime_matches(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """Compose runtime matches slip but working-tree blob hash drifted -> reseal only."""
    added, blob_state = _wire_git
    blob_state["value"] = "edited_tree_hash"
    config_dir, project_root, _ = _setup_promote_env(
        tmp_path,
        release_mode="upgrade",
        slip_compose_tree="original_tree_hash",
    )
    # Simulate operator comment edit: append a comment; runtime unchanged.
    compose_path = project_root / "docker-compose.yml"
    compose_path.write_text(
        compose_path.read_text(encoding="utf-8") + "# operator note\n",
        encoding="utf-8",
    )

    code = run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")

    assert code == 0
    # Compose file NOT modified by reconciliation (only the operator edit).
    final_compose = compose_path.read_text(encoding="utf-8")
    assert final_compose.endswith("# operator note\n")

    # Slip re-sealed with new tree hash.
    copied = tomllib.loads((project_root / "sync" / "packing_slips" / "v1.0.0.toml").read_text())
    assert copied["inputs"]["git"]["compose_tree"] == "edited_tree_hash"

    # Compose not staged (reconciliation did not change it).
    added_rel = {str(p.relative_to(project_root)) for p in added}
    assert "docker-compose.yml" not in added_rel


def test_upgrade_mode_resealed_slip_reloads_cleanly(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """The re-sealed slip parses cleanly and its content_sha256 verifies."""
    _, blob_state = _wire_git
    blob_state["value"] = "new_tree_hash"
    config_dir, project_root, _ = _setup_promote_env(
        tmp_path,
        release_mode="upgrade",
        slip_compose_tree="old_tree_hash",
    )

    run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")

    # load_slip re-verifies the content_sha256; raises on corruption.
    loaded = slip_mod.load_slip(project_root / "sync" / "packing_slips" / "v1.0.0.toml")
    assert loaded.inputs_git.compose_tree == "new_tree_hash"


def test_upgrade_mode_reseal_preserves_other_fields(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """Re-sealing preserves runtime and other tree hashes; release_id becomes the tag.

    Slice 10 Turn 0: promote re-seals the release slip with
    ``release_id = tag``. The candidate slip keeps ``release_id = <run-id>``
    (identity is the resolver run, not the operator's tag); every other
    field is untouched by the re-seal.
    """
    _, blob_state = _wire_git
    blob_state["value"] = "new_tree_hash"
    config_dir, project_root, _ = _setup_promote_env(
        tmp_path,
        release_mode="upgrade",
        slip_compose_tree="old_tree_hash",
    )

    run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")

    copied = tomllib.loads((project_root / "sync" / "packing_slips" / "v1.0.0.toml").read_text())
    assert copied["release_id"] == "v1.0.0"
    assert copied["runtime"]["minecraft_version"] == "1.21.1"
    assert copied["runtime"]["loader_type"] == "forge"
    assert copied["runtime"]["loader_version"] == "52.0.1"
    assert copied["inputs"]["git"]["config_tree"] == _hex("2")


# ---------------------------------------------------------------------------
# Upgrade mode: preconditions
# ---------------------------------------------------------------------------


def test_upgrade_mode_missing_compose_raises(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """Missing docker-compose.yml -> exit 3 before any writes or staging."""
    added, _ = _wire_git
    config_dir, project_root, _ = _setup_promote_env(
        tmp_path,
        release_mode="upgrade",
        write_compose=False,
    )

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")
    assert ei.value.exit_code == 3
    assert "compose file not found" in str(ei.value)

    assert not (project_root / "sync" / "packing_slips" / "v1.0.0.toml").exists()
    assert added == []


def test_upgrade_mode_no_minecraft_service_raises(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """A compose file with no minecraft service -> exit 3, no staging."""
    added, _ = _wire_git
    config_dir, project_root, _ = _setup_promote_env(
        tmp_path,
        release_mode="upgrade",
        compose_body="services:\n  web:\n    image: nginx:latest\n",
    )

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")
    assert ei.value.exit_code == 3
    assert "no service declares an image containing 'minecraft'" in str(ei.value)
    assert added == []


def test_upgrade_mode_multiple_minecraft_services_raises(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """Two minecraft services -> exit 3, no staging."""
    added, _ = _wire_git
    body = (
        "services:\n"
        "  mc-a:\n"
        "    image: itzg/minecraft-server\n"
        "    environment:\n"
        '      VERSION: "1.21.1"\n'
        "  mc-b:\n"
        "    image: itzg/minecraft-server\n"
        "    environment:\n"
        '      VERSION: "1.21.1"\n'
    )
    config_dir, project_root, _ = _setup_promote_env(
        tmp_path,
        release_mode="upgrade",
        compose_body=body,
    )

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")
    assert ei.value.exit_code == 3
    assert "2 services declare minecraft images" in str(ei.value)
    assert added == []


# ---------------------------------------------------------------------------
# Candidate-selection behaviors (unchanged from prior stubs)
# ---------------------------------------------------------------------------


def test_no_candidate_raises(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """No candidate directories -> exit 3, no staging."""
    added, _ = _wire_git
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{state_dir}"\n',
        encoding="utf-8",
    )

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=tmp_path, tag="v1.0.0")
    assert ei.value.exit_code == 3
    assert "no materialized candidate" in str(ei.value)
    assert added == []


def test_multiple_candidates_raises(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """Two candidate directories -> exit 3, no staging."""
    added, _ = _wire_git
    config_dir, project_root, _ = _setup_promote_env(tmp_path)
    second = project_root / "state" / "candidates" / "run-2"
    second.mkdir(parents=True)
    (second / "packing_slip.toml").write_text("placeholder")

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")
    assert ei.value.exit_code == 3
    assert "multiple materialized candidates" in str(ei.value)
    assert added == []


def test_refuses_to_overwrite_different_slip(tmp_path: Path, _wire_git: tuple[list[Path], dict[str, str]]) -> None:
    """An existing slip with different bytes at the destination -> exit 3, no staging."""
    added, _ = _wire_git
    config_dir, project_root, _ = _setup_promote_env(tmp_path)
    (project_root / "sync" / "packing_slips" / "v1.0.0.toml").write_text("different content", encoding="utf-8")

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")
    assert ei.value.exit_code == 3
    assert "refusing to overwrite" in str(ei.value)
    assert added == []


# ---------------------------------------------------------------------------
# Integration tests: real _git_add / _git_blob_hash against a real repo
# ---------------------------------------------------------------------------
#
# The `_wire_git` fixture used above monkeypatches `_git_add` and
# `_git_blob_hash`, so those function bodies were not exercised by the
# behavior tests above (which is why the coverage report flagged them at
# ~10%). These tests call the functions directly against a real git
# repository. No monkeypatch of the internal helpers; `subprocess.run` is
# used only as ground-truth verification, not as a replacement for the
# code under test.
#
# PlatformCore.git.stage_paths / hash_object are the shared implementation
# this migration targets (Slice 9 Turn C1); these tests pin the application
# adapter layer that translates their exceptions into UpgradeCliError(3).


def _init_git_repo(repo: Path) -> None:
    """Initialize a real git repo with a local identity (no global config needed)."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)


@pytest.fixture
def real_git_repo(tmp_path: Path) -> Path:
    """Provide a real, initialized git repo under tmp_path."""
    repo = tmp_path / "git-repo"
    repo.mkdir()
    _init_git_repo(repo)
    return repo


def test_git_add_stages_files(real_git_repo: Path) -> None:
    """_git_add stages files via PlatformCore.git.stage_paths."""
    (real_git_repo / "a.txt").write_bytes(b"a")
    (real_git_repo / "sub").mkdir()
    (real_git_repo / "sub" / "b.txt").write_bytes(b"b")

    promote._git_add(
        real_git_repo,
        [real_git_repo / "a.txt", real_git_repo / "sub" / "b.txt"],
    )

    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=real_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert sorted(result.stdout.splitlines()) == ["a.txt", "sub/b.txt"]


def test_git_add_empty_list_is_noop(real_git_repo: Path) -> None:
    """An empty paths list makes no git call and stages nothing."""
    promote._git_add(real_git_repo, [])

    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=real_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""


def test_git_add_translates_calledprocesserror(real_git_repo: Path) -> None:
    """A git failure (nonexistent path) is translated to UpgradeCliError(3)."""
    with pytest.raises(UpgradeCliError) as ei:
        promote._git_add(real_git_repo, [real_git_repo / "does-not-exist.txt"])
    assert ei.value.exit_code == 3
    assert "git add failed" in str(ei.value)


def test_git_add_translates_filenotfound(real_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing git binary (FileNotFoundError) is translated to UpgradeCliError(3)."""

    def _raise(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("simulated: git binary not found")

    monkeypatch.setattr(promote, "stage_paths", _raise)

    with pytest.raises(UpgradeCliError) as ei:
        promote._git_add(real_git_repo, [real_git_repo / "a.txt"])
    assert ei.value.exit_code == 3
    assert "git add failed" in str(ei.value)
    assert "simulated: git binary not found" in str(ei.value)


def test_git_blob_hash_returns_real_hash(real_git_repo: Path) -> None:
    """_git_blob_hash returns the same hash git itself computes."""
    target = real_git_repo / "content.bin"
    target.write_bytes(b"the quick brown fox")

    wrapper_hash = promote._git_blob_hash(real_git_repo, "content.bin")

    raw = subprocess.run(
        ["git", "hash-object", "content.bin"],
        cwd=real_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert wrapper_hash == raw.stdout.strip()
    assert len(wrapper_hash) == 40


def test_git_blob_hash_translates_calledprocesserror(real_git_repo: Path) -> None:
    """A git failure (nonexistent file) is translated to UpgradeCliError(3)."""
    with pytest.raises(UpgradeCliError) as ei:
        promote._git_blob_hash(real_git_repo, "does-not-exist.bin")
    assert ei.value.exit_code == 3
    assert "git hash-object failed" in str(ei.value)
    assert "does-not-exist.bin" in str(ei.value)


def test_git_blob_hash_translates_filenotfound(real_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing git binary (FileNotFoundError) is translated to UpgradeCliError(3)."""

    def _raise(*args: object, **kwargs: object) -> str:
        raise FileNotFoundError("simulated: git binary not found")

    monkeypatch.setattr(promote, "hash_object", _raise)

    with pytest.raises(UpgradeCliError) as ei:
        promote._git_blob_hash(real_git_repo, "content.bin")
    assert ei.value.exit_code == 3
    assert "git hash-object unavailable" in str(ei.value)
    assert "simulated: git binary not found" in str(ei.value)


def test_git_blob_hash_empty_output_translated(real_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty hash result (defensive) is translated to UpgradeCliError(3)."""
    monkeypatch.setattr(promote, "hash_object", lambda *a, **kw: "")

    with pytest.raises(UpgradeCliError) as ei:
        promote._git_blob_hash(real_git_repo, "content.bin")
    assert ei.value.exit_code == 3
    assert "empty output" in str(ei.value)


# ---------------------------------------------------------------------------
# Slice 10 Turn 0: release slip's release_id is the tag, not the run-id
# ---------------------------------------------------------------------------
#
# The candidate slip uses release_id = <run-id> (set by materialize; identity
# of the resolver run). The release slip uses release_id = <tag> (set by
# promote; identity that the release registry, publication, and Phase B
# approval all bind to). Upgrade_System_specifications Section 5.2 and
# Section 5.3 rule 1.  Covered for both modes because the re-seal is
# unconditional in the release path.


@pytest.mark.parametrize("release_mode", ["update", "upgrade"])
def test_release_slip_has_tag_as_release_id(
    tmp_path: Path,
    _wire_git: tuple[list[Path], dict[str, str]],
    release_mode: str,
) -> None:
    """The release slip's release_id is the operator-chosen tag, not the run-id."""
    _, _ = _wire_git
    config_dir, project_root, run_id = _setup_promote_env(tmp_path, release_mode=release_mode)

    run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")

    # Release slip: release_id == tag.
    released = slip_mod.load_slip(project_root / "sync" / "packing_slips" / "v1.0.0.toml")
    assert released.release_id == "v1.0.0"

    # Candidate slip: release_id == run-id (unchanged identity).
    candidate = slip_mod.load_slip(project_root / "state" / "candidates" / run_id / "packing_slip.toml")
    assert candidate.release_id == run_id


@pytest.mark.parametrize("release_mode", ["update", "upgrade"])
def test_release_slip_content_sha256_verifies_after_reseal(
    tmp_path: Path,
    _wire_git: tuple[list[Path], dict[str, str]],
    release_mode: str,
) -> None:
    """The re-sealed release slip's content_sha256 verifies (load_slip succeeds)."""
    _, blob_state = _wire_git
    blob_state["value"] = "consistent_tree_hash"
    config_dir, project_root, _ = _setup_promote_env(
        tmp_path,
        release_mode=release_mode,
        slip_compose_tree="consistent_tree_hash",
    )

    run(config_dir=config_dir, project_root=project_root, tag="v1.0.0")

    # load_slip recomputes content_sha256 over the body and raises on
    # mismatch; a clean load is proof the re-seal recomputed it correctly.
    released = slip_mod.load_slip(project_root / "sync" / "packing_slips" / "v1.0.0.toml")
    assert released.release_id == "v1.0.0"
    assert len(released.content_sha256) == 64
