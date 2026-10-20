# tests/modpack_upgrade/test_build_runner.py

"""Unit tests for ``build.runner`` -- clone, build, stage.

Every test creates a real local git repository under ``tmp_path`` and a
real shell script. This exercises the runner's subprocess handling
against the actual ``git`` and ``bash`` binaries, not mocks.

Tests skip cleanly when ``git`` or ``bash`` is not available on the
host's PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from minecraft.modpack_upgrade.build.runner import run_build
from minecraft.modpack_upgrade.build.spec import BuildArtifact, BuildSpec

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


def _commit_all(repo: Path, message: str = "commit") -> None:
    """Stage and commit everything in ``repo``."""
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def _write_script(repo: Path, name: str, body: str) -> None:
    """Write an executable shell script at ``repo/<name>``."""
    script = repo / name
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)


def _make_repo(
    tmp_path: Path,
    *,
    tag: str = "v1.0.0",
    script_body: str | None = None,
) -> Path:
    """Create a source repo with a build script and one commit."""
    repo = tmp_path / "source"
    _init_git_repo(repo)
    default_script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "mkdir -p build/libs build/.index\n"
        "echo 'jar-content' > build/libs/mod-1.0.0.jar\n"
        "echo 'pw-toml' > build/.index/mod.pw.toml\n"
    )
    _write_script(repo, "build.sh", script_body if script_body is not None else default_script)
    _commit_all(repo)
    subprocess.run(["git", "tag", tag], cwd=repo, check=True)
    return repo


def _spec(repo: Path, *, tag: str = "v1.0.0", **overrides: object) -> BuildSpec:
    """Return a spec pointing at ``repo`` with defaults."""
    base: dict[str, object] = {
        "name": "mod",
        "variables": {},
        "repository": str(repo),
        "ref": tag,
        "entrypoint": "bash",
        "script": "build.sh",
        "artifacts": (
            BuildArtifact(path="build/libs/*.jar"),
            BuildArtifact(path="build/.index/*.pw.toml"),
        ),
    }
    base.update(overrides)
    return BuildSpec(**base)  # type: ignore[arg-type]


def _dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (project_root, workspace_root, warehouse_root) rooted under tmp_path."""
    project = tmp_path / "project"
    project.mkdir()
    return (project, project / ".build-workspace", project / "sync" / "downloads")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_stages_jar_and_pw_toml(tmp_path: Path) -> None:
    """A successful build copies the jar and pw.toml to their destinations."""
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
    assert (warehouse / ".index" / "mod.pw.toml").is_file()
    assert str(warehouse / "mod-1.0.0.jar") in outcome.staged_artifacts
    assert str(warehouse / ".index" / "mod.pw.toml") in outcome.staged_artifacts


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


def test_dest_override_routes_to_custom_path(tmp_path: Path) -> None:
    """A ``dest`` override routes an artifact to a project-relative path."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)
    spec = _spec(
        repo,
        artifacts=(BuildArtifact(path="build/libs/*.jar", dest="custom/place.jar"),),
    )

    run_build(spec, project_root=project, workspace_root=workspace, warehouse_root=warehouse)

    assert (project / "custom" / "place.jar").is_file()


# ---------------------------------------------------------------------------
# Clone / checkout failures
# ---------------------------------------------------------------------------


def test_clone_failure_returns_outcome(tmp_path: Path) -> None:
    """A nonexistent repository yields a failed outcome naming the failure."""
    project, workspace, warehouse = _dirs(tmp_path)
    spec = _spec(
        tmp_path / "does-not-exist",
        artifacts=(BuildArtifact(path="build/*.jar"),),
    )

    outcome = run_build(spec, project_root=project, workspace_root=workspace, warehouse_root=warehouse)

    assert outcome.success is False
    assert "git clone failed" in (outcome.failure_message or "")
    assert not (warehouse / "mod-1.0.0.jar").exists()


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
    body = "#!/usr/bin/env bash\nexit 42\n"
    repo = _make_repo(tmp_path, script_body=body)
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(repo, artifacts=(BuildArtifact(path="build/*.jar"),)),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    assert outcome.success is False
    assert "script exited with 42" in (outcome.failure_message or "")
    assert outcome.clone_dir.is_dir()


def test_script_timeout_returns_failure(tmp_path: Path) -> None:
    """A script that exceeds the timeout yields a failed outcome."""
    body = "#!/usr/bin/env bash\nsleep 2\n"
    repo = _make_repo(tmp_path, script_body=body)
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(
            repo,
            timeout_seconds=0.3,
            artifacts=(BuildArtifact(path="build/*.jar"),),
        ),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    assert outcome.success is False
    assert "timed out" in (outcome.failure_message or "")


def test_entrypoint_not_found_returns_failure(tmp_path: Path) -> None:
    """A missing entrypoint yields a failed outcome naming it."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(
            repo,
            entrypoint="nonexistent-binary-xyz-12345",
            artifacts=(BuildArtifact(path="build/*.jar"),),
        ),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    assert outcome.success is False
    assert "not found on PATH" in (outcome.failure_message or "")


def test_working_dir_missing_returns_failure(tmp_path: Path) -> None:
    """A nonexistent working_dir yields a failed outcome."""
    repo = _make_repo(tmp_path)
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(
            repo,
            working_dir="does-not-exist",
            artifacts=(BuildArtifact(path="build/*.jar"),),
        ),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    assert outcome.success is False
    assert "working directory does not exist" in (outcome.failure_message or "")


# ---------------------------------------------------------------------------
# Artifact-pattern failures
# ---------------------------------------------------------------------------


def test_artifact_no_match_returns_failure(tmp_path: Path) -> None:
    """A pattern that matches no file yields a failed outcome."""
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


def test_artifact_multiple_matches_returns_failure(tmp_path: Path) -> None:
    """A pattern matching more than one file yields a failed outcome."""
    extra_script = "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p build/libs\necho a > build/libs/a-1.0.0.jar\necho b > build/libs/b-1.0.0.jar\n"
    repo = _make_repo(tmp_path, script_body=extra_script)
    project, workspace, warehouse = _dirs(tmp_path)

    outcome = run_build(
        _spec(repo, artifacts=(BuildArtifact(path="build/libs/*.jar"),)),
        project_root=project,
        workspace_root=workspace,
        warehouse_root=warehouse,
    )

    assert outcome.success is False
    assert "matched 2 files" in (outcome.failure_message or "")


def test_artifact_traversal_rejected(tmp_path: Path) -> None:
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
