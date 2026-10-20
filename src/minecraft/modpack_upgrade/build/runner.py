# src/minecraft/modpack_upgrade/build/runner.py

"""Build runner: clone repo, run the mod's build, stage artifacts.

Never raises on operational failure -- returns a structured
:class:`BuildOutcome`. The caller (the CLI command) translates failure
into the appropriate exit code and diagnostic.

Cleanup policy
--------------
On success, the clone directory is removed. On any failure, it is kept
so the operator can inspect ``gradle`` logs, build output, or partial
artifacts. The path is named in the failure outcome and in the log.

Shallow clones
--------------
When ``spec.depth > 0``, the runner uses ``git clone --depth N
--branch <ref>``, which requires ``ref`` to be a branch or tag name.
Commit SHA refs require ``depth=0``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from LoggingCore import get_logger

from minecraft.modpack_upgrade.build.spec import BuildArtifact, BuildSpec

__all__ = ["BuildOutcome", "run_build"]


_log = get_logger(__name__)


_EXCERPT_LIMIT = 2000
_GIT_CLONE_TIMEOUT_SECONDS = 300
_GIT_CHECKOUT_TIMEOUT_SECONDS = 60


@dataclass
class BuildOutcome:
    """Result of :func:`run_build`."""

    success: bool
    name: str
    clone_dir: Path
    staged_artifacts: dict[str, Path] = field(default_factory=dict)
    failure_message: str | None = None
    stdout_excerpt: str | None = None
    stderr_excerpt: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tail(text: str) -> str:
    """Return at most ``_EXCERPT_LIMIT`` characters of ``text``, taken from the tail."""
    if len(text) <= _EXCERPT_LIMIT:
        return text
    return "..." + text[-_EXCERPT_LIMIT:]


def _make_clone_path(workspace_root: Path, name: str) -> Path:
    """Return a unique clone path under ``workspace_root`` (already created)."""
    workspace_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{name}-", dir=str(workspace_root)))


def _clone_repository(spec: BuildSpec, clone_dir: Path) -> tuple[bool, str | None]:
    """Clone ``spec.repository`` into ``clone_dir``.

    When ``spec.depth > 0``, uses ``git clone --depth N --branch <ref>``,
    which requires ``ref`` to be a branch or tag name.
    """
    if spec.depth > 0:
        args = [
            "git",
            "clone",
            "--depth",
            str(spec.depth),
            "--branch",
            spec.ref,
            spec.repository,
            str(clone_dir),
        ]
    else:
        args = ["git", "clone", spec.repository, str(clone_dir)]
    try:
        subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_GIT_CLONE_TIMEOUT_SECONDS,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()[:300]
        return (False, f"git clone failed ({exc.returncode}): {stderr}")
    except subprocess.TimeoutExpired:
        return (False, f"git clone timed out after {_GIT_CLONE_TIMEOUT_SECONDS}s")
    except FileNotFoundError:
        return (False, "git is not installed")
    return (True, None)


def _checkout_ref(spec: BuildSpec, clone_dir: Path) -> tuple[bool, str | None]:
    """Checkout ``spec.ref`` in ``clone_dir``.

    Skipped when the clone was shallow (``--branch`` already selected
    the ref).
    """
    if spec.depth > 0:
        return (True, None)
    try:
        subprocess.run(
            ["git", "checkout", "--detach", spec.ref],
            cwd=str(clone_dir),
            capture_output=True,
            text=True,
            timeout=_GIT_CHECKOUT_TIMEOUT_SECONDS,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()[:300]
        return (False, f"git checkout {spec.ref!r} failed: {stderr}")
    except subprocess.TimeoutExpired:
        return (False, f"git checkout timed out after {_GIT_CHECKOUT_TIMEOUT_SECONDS}s")
    except FileNotFoundError:
        return (False, "git is not installed")
    return (True, None)


def _run_script(spec: BuildSpec, clone_dir: Path) -> tuple[bool, str | None, str, str]:
    """Run the build script; return ``(ok, error, stdout, stderr)``."""
    work_dir = clone_dir / spec.working_dir
    if not work_dir.is_dir():
        return (False, f"working directory does not exist: {work_dir}", "", "")
    argv = [spec.entrypoint, spec.script, *spec.args]
    env = {**os.environ, **spec.env}
    try:
        result = subprocess.run(
            argv,
            cwd=str(work_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=spec.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return (False, f"script timed out after {spec.timeout_seconds}s", stdout, stderr)
    except FileNotFoundError:
        return (False, f"entrypoint {spec.entrypoint!r} not found on PATH", "", "")
    if result.returncode != 0:
        return (
            False,
            f"script exited with {result.returncode}",
            result.stdout or "",
            result.stderr or "",
        )
    return (True, None, result.stdout or "", result.stderr or "")


def _expand_artifact(pattern: str, clone_dir: Path) -> tuple[Path | None, str | None]:
    """Expand ``pattern`` under ``clone_dir``; exactly one match required.

    Rejects patterns containing ``..`` (path traversal in the pattern
    itself) and matches that resolve outside ``clone_dir`` (symlinks
    pointing elsewhere). The mod is trusted to run arbitrary code, so
    this is defense-in-depth rather than a security boundary.
    """
    if ".." in Path(pattern).parts:
        return (None, f"artifact pattern {pattern!r} escapes the clone directory")
    matches = sorted(clone_dir.glob(pattern))
    if not matches:
        return (None, f"no files matched pattern {pattern!r}")
    if len(matches) > 1:
        names = [str(m.relative_to(clone_dir)) for m in matches]
        return (None, f"pattern {pattern!r} matched {len(matches)} files: {names}")
    resolved = matches[0].resolve()
    try:
        resolved.relative_to(clone_dir.resolve())
    except ValueError:
        return (None, f"artifact {matches[0].relative_to(clone_dir)} resolves outside the clone directory")
    return (matches[0], None)


def _destination_for(
    artifact: BuildArtifact,
    matched: Path,
    *,
    project_root: Path,
    warehouse_root: Path,
) -> Path:
    """Return the destination for ``matched`` per the artifact's ``dest`` rules."""
    if artifact.dest is not None:
        dest = Path(artifact.dest)
        if not dest.is_absolute():
            dest = project_root / dest
        return dest
    if matched.name.endswith(".pw.toml"):
        return warehouse_root / ".index" / matched.name
    return warehouse_root / matched.name


def _stage_artifacts(
    spec: BuildSpec,
    clone_dir: Path,
    *,
    project_root: Path,
    warehouse_root: Path,
) -> tuple[dict[str, Path], str | None]:
    """Copy every matched artifact to its destination; return the mapping."""
    staged: dict[str, Path] = {}
    for artifact in spec.artifacts:
        matched, err = _expand_artifact(artifact.path, clone_dir)
        if err is not None:
            return ({}, err)
        assert matched is not None
        dest = _destination_for(
            artifact,
            matched,
            project_root=project_root,
            warehouse_root=warehouse_root,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(matched, dest)
        staged[str(dest)] = matched
    return (staged, None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_build(
    spec: BuildSpec,
    *,
    project_root: Path,
    workspace_root: Path,
    warehouse_root: Path,
    keep_on_success: bool = False,
) -> BuildOutcome:
    """Execute one in-house build end-to-end.

    Steps: clone, checkout ref, run entrypoint+script, expand artifact
    patterns, copy to destinations. On any failure the clone is kept for
    inspection; on success (unless ``keep_on_success``) it is removed.

    Never raises on operational failure; the outcome carries the state.
    """
    clone_dir = _make_clone_path(workspace_root, spec.name)
    _log.info(f"build: {spec.name} -> {clone_dir} (ref {spec.ref!r})")

    ok, err = _clone_repository(spec, clone_dir)
    if not ok:
        return BuildOutcome(False, spec.name, clone_dir, failure_message=err)

    ok, err = _checkout_ref(spec, clone_dir)
    if not ok:
        return BuildOutcome(False, spec.name, clone_dir, failure_message=err)

    ok, err, stdout, stderr = _run_script(spec, clone_dir)
    if not ok:
        return BuildOutcome(
            False,
            spec.name,
            clone_dir,
            failure_message=err,
            stdout_excerpt=_tail(stdout),
            stderr_excerpt=_tail(stderr),
        )

    staged, err = _stage_artifacts(
        spec,
        clone_dir,
        project_root=project_root,
        warehouse_root=warehouse_root,
    )
    if err is not None:
        return BuildOutcome(
            False,
            spec.name,
            clone_dir,
            failure_message=err,
            stdout_excerpt=_tail(stdout),
            stderr_excerpt=_tail(stderr),
        )

    if not keep_on_success:
        shutil.rmtree(clone_dir, ignore_errors=True)

    for dest_path in staged:
        _log.info(f"build: staged {dest_path}")
    return BuildOutcome(
        True,
        spec.name,
        clone_dir,
        staged_artifacts=staged,
        stdout_excerpt=_tail(stdout),
        stderr_excerpt=_tail(stderr),
    )
