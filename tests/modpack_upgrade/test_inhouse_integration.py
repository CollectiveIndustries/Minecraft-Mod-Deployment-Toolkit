# tests/modpack_upgrade/test_inhouse_integration.py

"""End-to-end T24 acceptance test: built in-house artifact flows into the
resolver as ``provider="in-house"``.

Exercises the full pipeline the operator runs:

  1. ``build-inhouse`` clones a repo, runs its build, stages the jar
     and the ``.pw.toml`` into the warehouse.
  2. The operator adds the jar to ``[in-house].filenames`` in
     ``upgrade.toml``.
  3. ``load_in_house_filenames`` returns it, sorted.
  4. ``load_seed`` classifies the seed entry as ``provider="in-house"``
     because the filename is in the in-house list.
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
from minecraft.modpack_upgrade.build.spec import BuildArtifact, BuildSpec
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
    """Create a local git repo that produces a real jar and pw.toml."""
    repo = tmp_path / "house-source"
    _init_git_repo(repo)
    stem = jar_name.removesuffix(".jar")
    script = repo / "build.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "mkdir -p build/libs build/.index\n"
        f"echo 'jar-bytes' > build/libs/{jar_name}\n"
        f'printf \'filename = "{jar_name}"\\nside = "both"\\n\' > build/.index/{stem}.pw.toml\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    subprocess.run(["git", "tag", "v1.0.0"], cwd=repo, check=True)
    return repo


def _spec(repo: Path, jar_name: str = "custom-mod-1.0.0.jar") -> BuildSpec:
    """Return a build spec for the local repo."""
    stem = jar_name.removesuffix(".jar")
    return BuildSpec(
        name="house",
        variables={},
        repository=str(repo),
        ref="v1.0.0",
        entrypoint="bash",
        script="build.sh",
        artifacts=(
            BuildArtifact(path=f"build/libs/{jar_name}"),
            BuildArtifact(path=f"build/.index/{stem}.pw.toml"),
        ),
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


# ---------------------------------------------------------------------------
# End-to-end pipeline
# ---------------------------------------------------------------------------


def test_build_stages_into_warehouse(tmp_path: Path) -> None:
    """The build populates the warehouse with the jar and pw.toml."""
    repo = _make_house_repo(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    warehouse = project / "sync" / "downloads"

    outcome = run_build(
        _spec(repo),
        project_root=project,
        workspace_root=project / ".build-workspace",
        warehouse_root=warehouse,
    )

    assert outcome.success is True
    assert (warehouse / "custom-mod-1.0.0.jar").is_file()
    assert (warehouse / ".index" / "custom-mod-1.0.0.pw.toml").is_file()


def test_built_jar_classified_in_house(tmp_path: Path) -> None:
    """After adding the jar to [in-house].filenames, load_seed classifies it as in-house."""
    repo = _make_house_repo(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    config_dir = project / "config.d"
    config_dir.mkdir()
    warehouse = project / "sync" / "downloads"

    # 1. Build.
    outcome = run_build(
        _spec(repo),
        project_root=project,
        workspace_root=project / ".build-workspace",
        warehouse_root=warehouse,
    )
    assert outcome.success is True

    # 2. Operator marks it in-house via upgrade.toml.
    (config_dir / "upgrade.toml").write_bytes(tomli_w.dumps({"in-house": {"filenames": ["custom-mod-1.0.0.jar"]}}).encode("utf-8"))

    # 3. load_in_house_filenames returns it.
    in_house = load_in_house_filenames(config_dir)
    assert in_house == ["custom-mod-1.0.0.jar"]

    # 4. load_seed classifies it as provider="in-house".
    seed = load_seed(warehouse / ".index", warehouse, in_house)
    assert len(seed.report_entries) == 1
    entry = seed.report_entries[0]
    assert entry.provider == "in-house"
    assert entry.filename == "custom-mod-1.0.0.jar"
    assert entry.project_id is None
    assert entry.current_version_id is None


def test_built_jar_participates_in_resolution_inputs(tmp_path: Path) -> None:
    """The in-house filename appears in resolution_inputs_sha256 and changes it."""
    repo = _make_house_repo(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    config_dir = project / "config.d"
    config_dir.mkdir()
    warehouse = project / "sync" / "downloads"

    run_build(
        _spec(repo),
        project_root=project,
        workspace_root=project / ".build-workspace",
        warehouse_root=warehouse,
    )

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
    project = tmp_path / "project"
    project.mkdir()
    config_dir = project / "config.d"
    config_dir.mkdir()
    warehouse = project / "sync" / "downloads"

    run_build(
        _spec(repo),
        project_root=project,
        workspace_root=project / ".build-workspace",
        warehouse_root=warehouse,
    )

    (config_dir / "upgrade.toml").write_bytes(tomli_w.dumps({"in-house": {"filenames": []}}).encode("utf-8"))

    seed = load_seed(warehouse / ".index", warehouse, [])
    assert seed.report_entries[0].provider == "unknown"
