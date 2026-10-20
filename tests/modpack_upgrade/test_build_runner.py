# tests/modpack_upgrade/test_build_runner.py

"""Unit tests for ``build.runner`` -- clone, build, generate .pw.toml, stage.

Every test creates a real local git repository under ``tmp_path`` and a
real shell script. Tests skip cleanly when ``git`` or ``bash`` is not
available on the host's PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from minecraft.modpack_upgrade.build.runner import run_build
from minecraft.modpack_upgrade.build.spec import BuildArtifact, BuildSpec, PwTomlDependency

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git not available"),
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available"),
]


# ---------------------------------------------------------------------------
# Local git fixture helpers
# ---------------------------------------------------------------------------


def _init_git_repo(repo: Path) -> None:
    """Initialize a git repo with local identity."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)


def _make_repo(
    tmp_path: Path,
    *,
    tag: str = "v1.0.0",
    script_body: str | None = None,
) -> Path:
    """Create a source repo with a build script that produces one jar."""
    repo = tmp_path / "source"
    _init_git_repo(repo)
    default_script = "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p build/libs\necho 'jar-content' > build/libs/mod-1.0.0.jar\n"
    script = repo / "build.sh"
    script.write_text(script_body if script_body is not None else default_script, encoding="utf-8")
    script.chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    subprocess.run(["git", "tag", tag], cwd=repo, check=True)
    return repo


def _spec(
    repo: Path,
    *,
    tag: str = "v1.0.0",
    side: str = "both",
    mod_name: str | None = None,
    dependencies: tuple[PwTomlDependency, ...] = (),
    **overrides: object,
) -> BuildSpec:
    """Return a spec pointing at ``repo`` with defaults."""
    base: dict[str, object] = {
        "name": "mod",
        "variables": {},
        "repository": str(repo),
        "ref": tag,
        "entrypoint": "bash",
        "script": "build.sh",
        "side": side,
        "artifacts": (BuildArtifact(path="build/libs/*.jar"),),
        "mod_name": mod_name,
        "dependencies": dependencies,
    }
    base.update(overrides)
    return BuildSpec(**base)  # type: ignore[arg-type]


def _dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (project_root, workspace_root, warehouse_root) rooted under tmp_path."""
    project = tmp_path / "project"
    project.mkdir()
    return (project, project / ".build-workspace", project / "sync" / "downloads")


def _read_pw_toml(path: Path) -> dict:
    """Parse a generated .pw.toml file."""
    with open(path, "rb") as fh:
        return tomllib.load(fh)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_stages_jar_and_generated_pw_toml(tmp_path: Path) -> None:
    """A successful build copies the jar and generates the .pw.toml."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(repo),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    assert outcome.success is True
    assert (warehouse / "mod-1.0.0.jar").is_file()
    assert (warehouse / ".index" / "mod-1.0.0.pw.toml").is_file()
    assert str(warehouse / "mod-1.0.0.jar") in outcome.staged_artifacts
    assert str(warehouse / ".index" / "mod-1.0.0.pw.toml") in outcome.staged_artifacts


def test_generated_pw_toml_contains_side(tmp_path: Path) -> None:
    """The generated .pw.toml's ``side`` field matches the build spec."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    run_build(
        _spec(repo, side="server"),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    parsed = _read_pw_toml(warehouse / ".index" / "mod-1.0.0.pw.toml")
    assert parsed["side"] == "server"


def test_generated_pw_toml_contains_filename(tmp_path: Path) -> None:
    """The generated .pw.toml's ``filename`` matches the built jar."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    run_build(_spec(repo), project_root=project, workspace_root=workspace, warehouse_root=warehouse)

    parsed = _read_pw_toml(warehouse / ".index" / "mod-1.0.0.pw.toml")
    assert parsed["filename"] == "mod-1.0.0.jar"


def test_generated_pw_toml_contains_mod_name(tmp_path: Path) -> None:
    """The generated .pw.toml's ``name`` matches the spec's ``mod_name``."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    run_build(
        _spec(repo, mod_name="Custom Mod"),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    parsed = _read_pw_toml(warehouse / ".index" / "mod-1.0.0.pw.toml")
    assert parsed["name"] == "Custom Mod"


def test_generated_pw_toml_contains_dependencies(tmp_path: Path) -> None:
    """The generated .pw.toml's dependency edges match the spec."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    run_build(
        _spec(repo, dependencies=(PwTomlDependency(addon_id="flywheel", type="required"),)),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    parsed = _read_pw_toml(warehouse / ".index" / "mod-1.0.0.pw.toml")
    assert parsed["x-prismlauncher-dependencies"] == [{"addonId": "flywheel", "type": "required"}]


def test_generated_pw_toml_has_no_provider_block(tmp_path: Path) -> None:
    """In-house .pw.toml has no ``[update.*]`` block, so ``provider = unknown``."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    run_build(_spec(repo), project_root=project, workspace_root=workspace, warehouse_root=warehouse)

    parsed = _read_pw_toml(warehouse / ".index" / "mod-1.0.0.pw.toml")
    assert "update" not in parsed
    assert "download" not in parsed


def test_happy_path_cleans_up_clone(tmp_path: Path) -> None:
    """On success, the clone directory is removed by default."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    run_build(_spec(repo), project_root=project, workspace_root=workspace, warehouse_root=warehouse)

    if workspace.is_dir():
        assert not any(workspace.iterdir())


def test_keep_on_success_preserves_clone(tmp_path: Path) -> None:
    """``keep_on_success=True`` leaves the clone in place for inspection."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(repo),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
        keep_on_success=True,
    )

    assert outcome.success is True
    assert outcome.clone_dir.is_dir()


# ---------------------------------------------------------------------------
# Clone / checkout failures
# ---------------------------------------------------------------------------


def test_clone_failure_returns_outcome(tmp_path: Path) -> None:
    """A nonexistent repository yields a failed outcome."""
    project, workspace, warehouse = _dirs(tmp_path)
    spec = _spec(tmp_path / "does-not-exist")

    outcome = run_build(spec, project_root=project, workspace_root=workspace, warehouse_root=warehouse)

    assert outcome.success is False
    assert "git clone failed" in (outcome.failure_message or "")


def test_checkout_failure_returns_outcome(tmp_path: Path) -> None:
    """A missing tag yields a failed outcome naming the checkout failure."""
    repo = _make_repo(tmp_path, tag="v1.0.0")
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(repo, tag="v9.9.9"),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    assert outcome.success is False
    assert "git checkout" in (outcome.failure_message or "")


# ---------------------------------------------------------------------------
# Script failures
# ---------------------------------------------------------------------------


def test_script_nonzero_exit_returns_failure(tmp_path: Path) -> None:
    """A script that exits non-zero yields a failed outcome."""
    repo = _make_repo(tmp_path, script_body="#!/usr/bin/env bash\nexit 42\n")
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(_spec(repo), project_root=project, workspace_root=workspace, warehouse_root=warehouse)

    assert outcome.success is False
    assert "script exited with 42" in (outcome.failure_message or "")
    assert outcome.clone_dir.is_dir()


def test_script_timeout_returns_failure(tmp_path: Path) -> None:
    """A script that exceeds the timeout yields a failed outcome."""
    repo = _make_repo(tmp_path, script_body="#!/usr/bin/env bash\nsleep 2\n")
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(repo, timeout_seconds=0.3),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    assert outcome.success is False
    assert "timed out" in (outcome.failure_message or "")


def test_entrypoint_not_found_returns_failure(tmp_path: Path) -> None:
    """A missing entrypoint yields a failed outcome."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(repo, entrypoint="nonexistent-binary-xyz-12345"),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    assert outcome.success is False
    assert "not found on PATH" in (outcome.failure_message or "")


# ---------------------------------------------------------------------------
# Artifact selection failures
# ---------------------------------------------------------------------------


def test_no_artifact_matched_returns_failure(tmp_path: Path) -> None:
    """A pattern matching no file yields a failed outcome."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(repo, artifacts=(BuildArtifact(path="build/never/*.jar"),)),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    assert outcome.success is False
    assert "no files matched" in (outcome.failure_message or "")


def test_single_pattern_matching_multiple_jars_returns_failure(tmp_path: Path) -> None:
    """One pattern matching two jars fails inside pattern expansion."""
    repo = _make_repo(
        tmp_path,
        script_body=("#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p build/libs\necho a > build/libs/a-1.0.0.jar\necho b > build/libs/b-1.0.0.jar\n"),
    )
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(_spec(repo), project_root=project, workspace_root=workspace, warehouse_root=warehouse)

    assert outcome.success is False
    assert "matched 2 files" in (outcome.failure_message or "")


def test_multiple_patterns_each_matching_one_jar_returns_failure(tmp_path: Path) -> None:
    """Two patterns each matching one distinct jar fail the exactly-one-jar rule."""
    repo = _make_repo(
        tmp_path,
        script_body=("#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p build/libs\necho a > build/libs/a-1.0.0.jar\necho b > build/libs/b-1.0.0.jar\n"),
    )
    project, workspace, warehouse = _dirs(tmp_path)
    spec = _spec(
        repo,
        artifacts=(
            BuildArtifact(path="build/libs/a-*.jar"),
            BuildArtifact(path="build/libs/b-*.jar"),
        ),
    )

    outcome = run_build(spec, project_root=project, workspace_root=workspace, warehouse_root=warehouse)

    assert outcome.success is False
    assert "expected exactly one artifact" in (outcome.failure_message or "")


def test_non_jar_artifact_matched_returns_failure(tmp_path: Path) -> None:
    """A matched file that isn't a ``.jar`` fails the artifact-type check."""
    repo = _make_repo(
        tmp_path,
        script_body=("#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p build/libs\necho x > build/libs/mod.txt\n"),
    )
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(repo, artifacts=(BuildArtifact(path="build/libs/*"),)),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    assert outcome.success is False
    assert "must be a .jar" in (outcome.failure_message or "")


def test_traversal_pattern_rejected(tmp_path: Path) -> None:
    """A pattern containing ``..`` is rejected before matching."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(repo, artifacts=(BuildArtifact(path="../*.jar"),)),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    assert outcome.success is False
    assert "escapes the clone directory" in (outcome.failure_message or "")


def test_duplicate_patterns_dedupe_to_single_jar(tmp_path: Path) -> None:
    """Two patterns matching the same jar dedupe to a single artifact."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)
    spec = _spec(
        repo,
        artifacts=(
            BuildArtifact(path="build/libs/*.jar"),
            BuildArtifact(path="build/libs/mod-1.0.0.jar"),
        ),
    )

    outcome = run_build(spec, project_root=project, workspace_root=workspace, warehouse_root=warehouse)

    assert outcome.success is True
    assert (warehouse / "mod-1.0.0.jar").is_file()
