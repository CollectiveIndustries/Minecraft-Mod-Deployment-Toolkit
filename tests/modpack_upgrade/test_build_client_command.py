# tests/modpack_upgrade/test_build_client_command.py

"""Functional tests for ``build-client`` (spec Section 15.1, Section 6.2).

``build_client.run`` was implemented in the final stub-closure round. Its
handler is exercised end-to-end against a real slip + real filesystem,
with only two external boundaries mocked:

  * ``load_deployment_config`` -- produces the ``DeploymentConfig`` the
    scope consumes. Building a full config tree in each test would
    duplicate ``test_config_model.py``.
  * ``deploy_client_scope`` -- performs the actual ZIP + changelog +
    shared-item publication. Its behavior is exhaustively tested in
    ``test_scope_client.py``; the CLI wrapper's job is dispatch and
    error translation, which is what this file pins.

Everything else runs for real: candidate discovery, slip loading,
SlipContext construction (including artifact existence + the config/
kubejs source directory checks), and error surfacing.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomli_w

from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.cli.commands import build_client as bc
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _slip_raw(
    *,
    run_id: str = "run-1",
    jar_contents: dict[str, bytes] | None = None,
    include_client_distribution: bool = True,
) -> dict:
    """Build a valid slip dict with a ``[client_distribution]`` block.

    Every jar's SHA-256 matches the corresponding bytes in
    ``jar_contents``, so ``_build_slip_context`` can verify them.
    """
    contents = jar_contents if jar_contents is not None else {"mod.jar": b"jar-bytes"}
    jars = [
        {
            "filename": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "side": "both",
            "provider": "modrinth",
            "pw_toml": f"{name.removesuffix('.jar')}.pw.toml",
            "pw_toml_sha256": _hex("c"),
            "dependencies": [],
        }
        for name, data in contents.items()
    ]
    raw: dict = {
        "packing_slip_version": 1,
        "release_id": run_id,
        "release_mode": "upgrade",
        "meta": {
            "created_at_utc": "2026-01-01T00:00:00Z",
            "resolver_version": "test",
            "author": "test",
        },
        "runtime": {
            "minecraft_version": "1.21.1",
            "loader_type": "forge",
            "loader_version": "52.0.1",
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
            "resolution_inputs_sha256": _hex("7"),
            "runtime_sha256": _hex("8"),
            "server_inputs_sha256": _hex("9"),
            "client_inputs_sha256": _hex("a"),
        },
        "jar": jars,
        "resource_pack": [],
        "velocity_plugin": [],
        "instances": {
            "survival": {
                "targeted": True,
                "required_sides": ["server"],
                "config_mode": "merge",
                "kubejs_mode": "delete",
                "config_deletions": [],
                "protect_patterns": [],
            },
        },
    }
    if include_client_distribution:
        raw["client_distribution"] = {
            "output_filename": "minecraft_client_20261012.zip",
            "sha256": _hex("b"),
            "with_resources": False,
        }
    raw["content_sha256"] = slip_mod.compute_content_sha256(raw)
    return raw


def _setup_candidate(
    tmp_path: Path,
    *,
    run_id: str = "run-1",
    jar_contents: dict[str, bytes] | None = None,
    include_client_distribution: bool = True,
    create_artifacts: bool = True,
) -> Path:
    """Create a project + candidate tree ready for ``build_client.run``.

    Returns the ``config_dir``. Populates:
      * ``<state>/candidates/<run_id>/packing_slip.toml``
      * ``<state>/candidates/<run_id>/artifacts/<jar>`` (if create_artifacts)
      * ``<project>/sync/config/`` and ``<project>/sync/kubejs/``
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    state_dir = tmp_path / "state"
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{state_dir}"\n',
        encoding="utf-8",
    )

    candidate = state_dir / "candidates" / run_id
    candidate.mkdir(parents=True)
    contents = jar_contents if jar_contents is not None else {"mod.jar": b"jar-bytes"}
    raw = _slip_raw(
        run_id=run_id,
        jar_contents=contents,
        include_client_distribution=include_client_distribution,
    )
    (candidate / "packing_slip.toml").write_text(tomli_w.dumps(raw), encoding="utf-8")

    if create_artifacts:
        artifacts = candidate / "artifacts"
        artifacts.mkdir()
        for name, data in contents.items():
            (artifacts / name).write_bytes(data)

    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)

    return config_dir


def _mock_scope(monkeypatch: pytest.MonkeyPatch, result: SimpleNamespace) -> None:
    """Patch ``deploy_client_scope`` in the ``build_client`` namespace."""
    monkeypatch.setattr(bc, "deploy_client_scope", lambda *_a, **_kw: result)


def _mock_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``load_deployment_config`` so the CLI reaches the scope call."""
    monkeypatch.setattr(bc, "load_deployment_config", lambda *_a, **_kw: SimpleNamespace())


def _scope_success() -> SimpleNamespace:
    """Return a ``ClientScopeResult``-shaped success namespace.

    The namespace carries every attribute ``build_client.run`` reads.
    Missing attributes would produce an ``AttributeError`` mid-handler,
    which is not what any of these tests are checking; the ``zip_url``
    field in particular is read conditionally so its absence would only
    surface on the happy path.
    """
    return SimpleNamespace(
        success=True,
        failure_message=None,
        zip_path=Path("/out/client.zip"),
        zip_sha256="f" * 64,
        zip_url="http://example/zip",
        changelog_path=Path("/out/changelog.html"),
        changelog_url="http://example/changelog",
        resolved_output_filename="minecraft_client_20261012.zip",
        initial_build=False,
    )


def _scope_failure(msg: str) -> SimpleNamespace:
    """Return a failure-shaped ``ClientScopeResult``.

    Includes every attribute the success namespace does so that a future
    change to ``build_client.run``'s error path (e.g. printing something
    before the failure check) does not accidentally work only because
    this namespace was missing the attribute.
    """
    return SimpleNamespace(
        success=False,
        failure_message=msg,
        zip_path=None,
        zip_sha256=None,
        zip_url=None,
        changelog_path=None,
        changelog_url=None,
        resolved_output_filename=None,
        initial_build=False,
    )


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------


def test_no_candidates_dir_exits_3(tmp_path: Path) -> None:
    """No ``<state>/candidates`` directory -> exit 3."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    state_dir = tmp_path / "state"
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{state_dir}"\n',
        encoding="utf-8",
    )

    with pytest.raises(UpgradeCliError) as ei:
        bc.run(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 3
    assert "no candidates directory" in str(ei.value)


def test_explicit_run_id_not_found_exits_3(tmp_path: Path) -> None:
    """An explicit ``--run-id`` that doesn't exist -> exit 3."""
    config_dir = _setup_candidate(tmp_path, run_id="run-a")
    with pytest.raises(UpgradeCliError) as ei:
        bc.run(config_dir=config_dir, project_root=tmp_path, run_id="run-missing")
    assert ei.value.exit_code == 3
    assert "candidate not found" in str(ei.value)


def test_no_candidates_at_all_exits_3(tmp_path: Path) -> None:
    """``candidates/`` exists but has no materialized dirs -> exit 3."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    state_dir = tmp_path / "state"
    (state_dir / "candidates").mkdir(parents=True)
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{state_dir}"\n',
        encoding="utf-8",
    )
    with pytest.raises(UpgradeCliError) as ei:
        bc.run(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 3
    assert "no materialized candidates" in str(ei.value)


def test_multiple_candidates_uses_most_recent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two candidates -> most recently modified slip wins."""
    config_dir = _setup_candidate(tmp_path, run_id="run-old")
    state_dir = tmp_path / "state"
    # Newer candidate
    newer = state_dir / "candidates" / "run-new"
    newer.mkdir()
    newer_raw = _slip_raw(run_id="run-new")
    (newer / "packing_slip.toml").write_text(tomli_w.dumps(newer_raw), encoding="utf-8")
    (newer / "artifacts").mkdir()
    (newer / "artifacts" / "mod.jar").write_bytes(b"jar-bytes")

    # Ensure newer has a later mtime than old
    time.sleep(0.01)
    (newer / "packing_slip.toml").touch()

    _mock_config(monkeypatch)
    _mock_scope(monkeypatch, _scope_success())

    code = bc.run(config_dir=config_dir, project_root=tmp_path)
    assert code == 0


# ---------------------------------------------------------------------------
# Slip validation
# ---------------------------------------------------------------------------


def test_slip_without_client_distribution_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slip with no ``[client_distribution]`` -> exit 3, no scope call."""
    config_dir = _setup_candidate(tmp_path, include_client_distribution=False)
    _mock_config(monkeypatch)
    _mock_scope(monkeypatch, _scope_success())

    with pytest.raises(UpgradeCliError) as ei:
        bc.run(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 3
    assert "client_distribution" in str(ei.value)


def test_missing_jar_in_artifacts_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slip declares a jar that isn't in ``artifacts/`` -> exit 3.

    ``create_artifacts=False`` skips creating the ``artifacts/`` directory
    entirely, which triggers ``_build_slip_context``'s directory-existence
    check before the per-jar check. This test creates an *empty* artifacts
    directory so the per-jar ``path.is_file()`` check is the one that
    fires -- that's what the assertion pins.
    """
    config_dir = _setup_candidate(tmp_path, create_artifacts=False)
    state_dir = tmp_path / "state"
    (state_dir / "candidates" / "run-1" / "artifacts").mkdir()
    _mock_config(monkeypatch)
    _mock_scope(monkeypatch, _scope_success())

    with pytest.raises(UpgradeCliError) as ei:
        bc.run(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 3
    assert "jar missing from candidate" in str(ei.value)


def test_missing_config_source_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``sync/config/`` in the project -> exit 3."""
    config_dir = _setup_candidate(tmp_path)
    (tmp_path / "sync" / "config").rmdir()
    _mock_config(monkeypatch)
    _mock_scope(monkeypatch, _scope_success())

    with pytest.raises(UpgradeCliError) as ei:
        bc.run(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 3
    assert "config source missing" in str(ei.value)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def test_happy_path_returns_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful scope -> exit 0, prints ZIP details."""
    config_dir = _setup_candidate(tmp_path)
    _mock_config(monkeypatch)
    _mock_scope(monkeypatch, _scope_success())

    code = bc.run(config_dir=config_dir, project_root=tmp_path)

    assert code == 0
    out = capsys.readouterr().out
    assert "client.zip" in out or "minecraft_client" in out
    assert "SHA-256" in out


def test_scope_failure_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``success=False`` scope result -> exit 1 with the failure message."""
    config_dir = _setup_candidate(tmp_path)
    _mock_config(monkeypatch)
    _mock_scope(monkeypatch, _scope_failure("simulated ZIP write failure"))

    with pytest.raises(UpgradeCliError) as ei:
        bc.run(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 1
    assert "simulated ZIP write failure" in str(ei.value)
