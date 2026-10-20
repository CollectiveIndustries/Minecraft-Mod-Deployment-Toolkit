# src/minecraft/modpack_upgrade/build/runner.py

"""Build runner: clone repo, run the mod's build, generate .pw.toml, stage.

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

Staging
-------
The runner always generates a ``.pw.toml`` for the single jar the
build produced, using the shared writer at
:mod:`minecraft.modpack_upgrade.pw_toml`. The jar lands at
``sync/downloads/<filename>``; the ``.pw.toml`` lands at
``sync/downloads/.index/<stem>.pw.toml``. The build spec's ``side``
becomes the ``.pw.toml``'s ``side`` field; ``name`` becomes ``name``;
``dependencies`` becomes ``[[x-prismlauncher-dependencies]]``.

This is the only staging path. Mods that generate their own ``.pw.toml``
must delete that step from their build script; the tool's generated
``.pw.toml`` is what the resolver sees.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from LoggingCore import get_logger

from minecraft.modpack_upgrade.build.spec import BuildSpec
from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.pw_toml import Dependency as PwTomlDependencyEntry
from minecraft.modpack_upgrade.pw_toml import PwTomlEntry, write_pw_toml

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
    """Clone ``spec.repository`` into ``clone_dir``."""
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
    """Checkout ``spec.ref`` in ``clone_dir``. Skipped when shallow."""
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
    """Expand ``pattern`` under ``clone_dir``; exactly one match required."""
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


def _collect_matched_artifacts(spec: BuildSpec, clone_dir: Path) -> tuple[list[Path], str | None]:
    """Expand every pattern; return the deduped matches or an error."""
    matched: list[Path] = []
    seen: set[Path] = set()
    for artifact in spec.artifacts:
        m, err = _expand_artifact(artifact.path, clone_dir)
        if err is not None:
            return ([], err)
        assert m is not None
        resolved = m.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        matched.append(m)
    return (matched, None)


def _select_single_jar(matched: list[Path], clone_dir: Path) -> tuple[Path | None, str | None]:
    """Validate that ``matched`` contains exactly one jar."""
    if len(matched) != 1:
        names = [str(m.relative_to(clone_dir)) for m in matched]
        return (None, f"expected exactly one artifact; patterns matched {len(matched)}: {names}")
    jar = matched[0]
    if not jar.name.endswith(".jar"):
        return (None, f"artifact must be a .jar; matched {jar.name!r}")
    return (jar, None)


def _write_generated_pw_toml(jar: Path, spec: BuildSpec, dest: Path) -> str | None:
    """Generate the .pw.toml for ``jar`` at ``dest``; return None on success."""
    entry = PwTomlEntry(
        filename=jar.name,
        side=spec.side,
        name=spec.mod_name,
        dependencies=tuple(
            PwTomlDependencyEntry(addon_id=d.addon_id, type=d.type)
            for d in spec.dependencies
        ),
    )
    try:
        write_pw_toml(entry, dest)
    except SchemaError as exc:
        return f"could not generate .pw.toml for {jar.name!r}: {exc}"
    except OSError as exc:
        return f"could not write .pw.toml for {jar.name!r} to {dest}: {exc}"
    return None


def _stage_artifacts(
    spec: BuildSpec,
    clone_dir: Path,
    *,
    warehouse_root: Path,
) -> tuple[dict[str, Path], str | None]:
    """Stage the single built jar and its generated .pw.toml."""
    matched, err = _collect_matched_artifacts(spec, clone_dir)
    if err is not None:
        return ({}, err)

    jar, err = _select_single_jar(matched, clone_dir)
    if err is not None or jar is None:
        return ({}, err or "no jar selected")

    staged: dict[str, Path] = {}

    jar_dest = warehouse_root / jar.name
    jar_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(jar, jar_dest)
    staged[str(jar_dest)] = jar

    index_dir = warehouse_root / ".index"
    index_dir.mkdir(parents=True, exist_ok=True)
    pw_toml_dest = index_dir / f"{jar.stem}.pw.toml"
    write_err = _write_generated_pw_toml(jar, spec, pw_toml_dest)
    if write_err is not None:
        return ({}, write_err)
    staged[str(pw_toml_dest)] = pw_toml_dest

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

    Steps: clone, checkout ref, run script, expand artifact patterns,
    validate exactly one jar, generate ``.pw.toml``, stage both files.

    On failure the clone is kept for inspection; on success (unless
    ``keep_on_success``) it is removed. Never raises on operational
    failure; the outcome carries the state.
    """
    del project_root  # staging is warehouse_root-relative; kept for API symmetry
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

    staged, err = _stage_artifacts(spec, clone_dir, warehouse_root=warehouse_root)
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
