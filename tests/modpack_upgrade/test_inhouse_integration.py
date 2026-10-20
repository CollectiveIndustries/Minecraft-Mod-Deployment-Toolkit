# tests/modpack_upgrade/test_inhouse_integration.py

"""End-to-end T24 acceptance test: built in-house artifact flows into the
resolver as ``provider="in-house"``.

Exercises the full pipeline the operator runs:

  1. ``build-inhouse`` clones the repo, runs its build, generates the
     ``.pw.toml`` from the build spec's ``side``, stages both into the
     warehouse.
  2. The operator adds the jar to ``[in-house].filenames`` in
     ``upgrade.toml``.
  3. ``load_seed`` classifies the seed entry as ``provider="in-house"``
     because the filename is in the in-house list.
  4. The seed entry's ``side_declared`` matches the build spec's ``side``
     (proving the declaration flows through the ``.pw.toml`` into the
     resolver input).
  5. ``resolution_inputs_sha256`` reflects the in-house filename.

Tests skip cleanly when ``git`` or ``bash`` is not available.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import tomli_w

from minecraft.modpack_upgrade.build.runner import run_build
from minecraft.modpack_upgrade.build.spec import BuildArtifact, BuildSpec, PwTomlDependency
from minecraft.modpack_upgrade.inhouse import load_in_house_filenames
from minecraft.modpack_upgrade.manifest import Manifest
from minecraft.modpack_upgrade.overrides import SideOverrides
from minecraft.modpack_upgrade.resolution_inputs import compute_resolution_inputs_sha256
from minecraft.modpack_upgrade.seed import load_seed

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git not available"),
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _init_git_repo(repo: Path) -> None:
    """Initialize a git repo with local identity."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)


def _make_house_repo(tmp_path: Path, *, jar_name: str = "custom-mod-1.0.0.jar") -> Path:
    """Create a local git repo that produces a real jar."""
    repo = tmp_path / "house-source"
    _init_git_repo(repo)
    script = repo / "build.sh"
    script.write_text(
        f"#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p build/libs\necho 'jar-bytes' > build/libs/{jar_name}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    subprocess.run(["git", "tag", "v1.0.0"], cwd=repo, check=True)
    return repo


def _spec(
    repo: Path,
    jar_name: str = "custom-mod-1.0.0.jar",
    *,
    side: str = "both",
    mod_name: str | None = None,
    dependencies: tuple[PwTomlDependency, ...] = (),
) -> BuildSpec:
    """Return a build spec for the local repo."""
    return BuildSpec(
        name="house",
        variables={},
        repository=str(repo),
        ref="v1.0.0",
        entrypoint="bash",
        script="build.sh",
        side=side,
        artifacts=(BuildArtifact(path=f"build/libs/{jar_name}"),),
        mod_name=mod_name,
        dependencies=dependencies,
    )


def _manifest() -> Manifest:
    """Return a minimal valid manifest."""
    return Manifest(
        minecraft_version="1.21.1",
        loader="forge",
        loader_version="52.0.1",
        selection="release_then_beta",
        no_version="ask",
        raw_sha256="0" * 64,
    )


def _build(tmp_path: Path, spec: BuildSpec) -> tuple[Path, Path, Path]:
    """Run the build; return (project, config_dir, warehouse)."""
    project = tmp_path / "project"
    project.mkdir()
    config_dir = project / "config.d"
    config_dir.mkdir()
    warehouse = project / "sync" / "downloads"
    outcome = run_build(
        spec,
        project_root=project,
        workspace_root=project / ".build-workspace",
        warehouse_root=warehouse,
    )
    assert outcome.success is True, outcome.failure_message
    return (project, config_dir, warehouse)


# ---------------------------------------------------------------------------
# End-to-end pipeline
# ---------------------------------------------------------------------------


def test_build_stages_jar_and_pw_toml(tmp_path: Path) -> None:
    """The build populates the warehouse with the jar and the generated .pw.toml."""
    repo = _make_house_repo(tmp_path)
    _project, _config_dir, warehouse = _build(tmp_path, _spec(repo))

    assert (warehouse / "custom-mod-1.0.0.jar").is_file()
    assert (warehouse / ".index" / "custom-mod-1.0.0.pw.toml").is_file()


def test_built_jar_classified_in_house(tmp_path: Path) -> None:
    """After adding the jar to [in-house].filenames, load_seed classifies it as in-house."""
    repo = _make_house_repo(tmp_path)
    _project, config_dir, warehouse = _build(tmp_path, _spec(repo))

    (config_dir / "upgrade.toml").write_bytes(tomli_w.dumps({"in-house": {"filenames": ["custom-mod-1.0.0.jar"]}}).encode("utf-8"))

    in_house = load_in_house_filenames(config_dir)
    assert in_house == ["custom-mod-1.0.0.jar"]

    seed = load_seed(warehouse / ".index", warehouse, in_house)
    assert len(seed.report_entries) == 1
    entry = seed.report_entries[0]
    assert entry.provider == "in-house"
    assert entry.filename == "custom-mod-1.0.0.jar"
    assert entry.project_id is None
    assert entry.current_version_id is None


def test_build_spec_side_lands_in_seed_side_declared(tmp_path: Path) -> None:
    """The build spec's ``side`` flows through the .pw.toml into ``side_declared``."""
    repo = _make_house_repo(tmp_path)
    _project, config_dir, warehouse = _build(tmp_path, _spec(repo, side="server"))

    (config_dir / "upgrade.toml").write_bytes(tomli_w.dumps({"in-house": {"filenames": ["custom-mod-1.0.0.jar"]}}).encode("utf-8"))

    in_house = load_in_house_filenames(config_dir)
    seed = load_seed(warehouse / ".index", warehouse, in_house)
    entry = seed.report_entries[0]
    assert entry.side_declared == "server"


def test_build_spec_dependencies_land_in_snapshot(tmp_path: Path) -> None:
    """The build spec's ``dependencies`` become ``.pw.toml`` edges that feed the snapshot."""
    repo = _make_house_repo(tmp_path)
    _project, config_dir, warehouse = _build(
        tmp_path,
        _spec(repo, dependencies=(PwTomlDependency(addon_id="flywheel", type="required"),)),
    )

    (config_dir / "upgrade.toml").write_bytes(tomli_w.dumps({"in-house": {"filenames": ["custom-mod-1.0.0.jar"]}}).encode("utf-8"))

    in_house = load_in_house_filenames(config_dir)
    seed = load_seed(warehouse / ".index", warehouse, in_house)
    snapshot_entry = seed.snapshot_entries[0]
    assert len(snapshot_entry.dependencies) == 1
    assert snapshot_entry.dependencies[0].addon_id == "flywheel"
    assert snapshot_entry.dependencies[0].type == "required"


def test_built_jar_participates_in_resolution_inputs(tmp_path: Path) -> None:
    """The in-house filename appears in resolution_inputs_sha256 and changes it."""
    repo = _make_house_repo(tmp_path)
    _project, config_dir, warehouse = _build(tmp_path, _spec(repo))

    (config_dir / "upgrade.toml").write_bytes(tomli_w.dumps({"in-house": {"filenames": ["custom-mod-1.0.0.jar"]}}).encode("utf-8"))

    in_house = load_in_house_filenames(config_dir)
    seed = load_seed(warehouse / ".index", warehouse, in_house)

    with_jar = compute_resolution_inputs_sha256(
        release_mode="upgrade",
        manifest=_manifest(),
        in_house_filenames=in_house,
        seed=seed.snapshot_entries,
        side_overrides=SideOverrides(),
    )

    without_jar = compute_resolution_inputs_sha256(
        release_mode="upgrade",
        manifest=_manifest(),
        in_house_filenames=[],
        seed=(),
        side_overrides=SideOverrides(),
    )

    assert with_jar != without_jar


def test_skip_in_house_list_classifies_as_unknown(tmp_path: Path) -> None:
    """Omitting the filename from [in-house] leaves provider detection to the pw.toml.

    The generated pw.toml declares no ``[update.*]`` block, so
    ``_extract_update_block`` classifies it as ``"unknown"``. This is the
    operator error the integration flow is designed to prevent: mark the
    artifact in-house or the resolver reports it as an unknown provider.
    """
    repo = _make_house_repo(tmp_path)
    _project, config_dir, warehouse = _build(tmp_path, _spec(repo))

    (config_dir / "upgrade.toml").write_bytes(tomli_w.dumps({"in-house": {"filenames": []}}).encode("utf-8"))

    seed = load_seed(warehouse / ".index", warehouse, [])
    assert seed.report_entries[0].provider == "unknown"
