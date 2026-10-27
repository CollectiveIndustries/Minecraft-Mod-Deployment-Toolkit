# tests/modpack_upgrade/test_snapshot_world_command.py

"""Functional tests for ``snapshot-world`` (spec Section 15.1, Section 10).

``snapshot_world.run`` takes an injectable ``runner`` (production uses
:func:`snapshot_world._default_runner`, which shells out to rsync; tests
inject a fake). Everything else runs for real:

  * ``[world_snapshot]`` config load
  * ssh key existence check
  * snapshot dir creation
  * manifest walk + canonical file list
  * snapshot_id (SHA-256 of the manifest bytes)

The fake runner simulates rsync by writing files into the target
directory and returning ``(0, "", "")``. That gives the manifest walker
real content to hash, so the ``snapshot_id`` assertion is meaningful.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import tomli_w

from minecraft.modpack_upgrade.cli.commands import snapshot_world as sw
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError

#: The ssh key file name that :func:`_setup` always creates inside
#: ``tmp_path``. Kept as a module-level constant so section builders can
#: reference the deterministic path without needing the ssh key itself
#: bound first.
_SSH_KEY_NAME = "id_deploy_world_ro"


def _setup(
    tmp_path: Path,
    *,
    section: dict | None = None,
    create_ssh_key: bool = True,
) -> tuple[Path, Path]:
    """Create ``config.d/upgrade.toml`` with a ``[world_snapshot]`` section.

    Returns ``(config_dir, ssh_key_path)``. The ssh key is always written
    at ``<tmp_path>/id_deploy_world_ro`` when ``create_ssh_key=True``,
    matching ``_SSH_KEY_NAME``.
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    ssh_key = tmp_path / _SSH_KEY_NAME
    if create_ssh_key:
        ssh_key.write_text("fake ssh key\n", encoding="utf-8")

    body: dict = {}
    if section is not None:
        body["world_snapshot"] = section
    (config_dir / "upgrade.toml").write_bytes(tomli_w.dumps(body).encode("utf-8"))
    return config_dir, ssh_key


def _full_section(tmp_path: Path) -> dict:
    """Return a complete ``[world_snapshot]`` section.

    The ``ssh_key`` path is deterministic -- :func:`_setup` always
    creates the key at ``tmp_path / _SSH_KEY_NAME``. Computing the path
    here (rather than taking ``ssh_key`` as a parameter) avoids the
    chicken-and-egg problem where the caller would have to call
    :func:`_setup` to obtain the key before it could build the section
    passed *to* :func:`_setup`.
    """
    return {
        "source_host": "prod.example.com",
        "source_user": "deploy",
        "source_path": "/srv/minecraft/survival/world",
        "ssh_key": str(tmp_path / _SSH_KEY_NAME),
        "dest_root": str(tmp_path / "snapshots"),
        "exclude": ["session.lock", "*.tmp", "*.lock"],
        "timeout_seconds": 60,
    }


def _full_section_override(tmp_path: Path, **overrides: object) -> dict:
    """Return ``_full_section(tmp_path)`` with the given fields overridden."""
    base = _full_section(tmp_path)
    base.update(overrides)
    return base


def _fake_rsync_success(files: dict[str, bytes]):
    """Return a runner that writes ``files`` into the rsync target directory.

    The target is derived from the last rsync argv element (with its
    trailing slash trimmed) so the runner behaves like real rsync: the
    files land where the command was told to write them.
    """

    def _runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
        del timeout
        target = Path(argv[-1].rstrip("/"))
        target.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            f = target / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(content)
        return (0, "", "")

    return _runner


def _fake_runner_returning(rc: int, stderr: str = ""):
    """Return a runner that just returns ``(rc, '', stderr)``."""

    def _runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
        del argv, timeout
        return (rc, "", stderr)

    return _runner


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_no_upgrade_toml_exits_3(tmp_path: Path) -> None:
    """No ``upgrade.toml`` at all -> exit 3 (missing source_host)."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    with pytest.raises(UpgradeCliError) as ei:
        sw.run(
            config_dir=config_dir,
            project_root=tmp_path,
            runner=_fake_runner_returning(0),
        )
    assert ei.value.exit_code == 3
    assert "source_host is required" in str(ei.value)


def test_missing_source_host_exits_3(tmp_path: Path) -> None:
    """Section without ``source_host`` -> exit 3."""
    config_dir, _ = _setup(tmp_path, section={"dest_root": str(tmp_path / "snap")})
    with pytest.raises(UpgradeCliError) as ei:
        sw.run(
            config_dir=config_dir,
            project_root=tmp_path,
            runner=_fake_runner_returning(0),
        )
    assert ei.value.exit_code == 3
    assert "source_host is required" in str(ei.value)


def test_invalid_exclude_list_exits_3(tmp_path: Path) -> None:
    """Non-list ``exclude`` -> exit 3."""
    config_dir, _ = _setup(
        tmp_path,
        section=_full_section_override(tmp_path, exclude="not-a-list"),
    )
    with pytest.raises(UpgradeCliError) as ei:
        sw.run(
            config_dir=config_dir,
            project_root=tmp_path,
            runner=_fake_runner_returning(0),
        )
    assert ei.value.exit_code == 3
    assert "exclude must be an array" in str(ei.value)


def test_non_integer_timeout_exits_3(tmp_path: Path) -> None:
    """Non-integer ``timeout_seconds`` -> exit 3."""
    config_dir, _ = _setup(
        tmp_path,
        section=_full_section_override(tmp_path, timeout_seconds="not-an-int"),
    )
    with pytest.raises(UpgradeCliError) as ei:
        sw.run(
            config_dir=config_dir,
            project_root=tmp_path,
            runner=_fake_runner_returning(0),
        )
    assert ei.value.exit_code == 3
    assert "timeout_seconds must be an integer" in str(ei.value)


def test_missing_ssh_key_exits_3(tmp_path: Path) -> None:
    """Missing ssh key file -> exit 3."""
    config_dir, _ = _setup(
        tmp_path,
        section=_full_section(tmp_path),
        create_ssh_key=False,
    )
    with pytest.raises(UpgradeCliError) as ei:
        sw.run(
            config_dir=config_dir,
            project_root=tmp_path,
            runner=_fake_runner_returning(0),
        )
    assert ei.value.exit_code == 3
    assert "ssh key not found" in str(ei.value)


# ---------------------------------------------------------------------------
# Runner failures
# ---------------------------------------------------------------------------


def test_rsync_nonzero_exit_exits_1(tmp_path: Path) -> None:
    """A non-zero rsync exit -> exit 1 with the stderr excerpt."""
    config_dir, _ = _setup(tmp_path, section=_full_section(tmp_path))
    with pytest.raises(UpgradeCliError) as ei:
        sw.run(
            config_dir=config_dir,
            project_root=tmp_path,
            runner=_fake_runner_returning(23, "partial transfer"),
        )
    assert ei.value.exit_code == 1
    assert "rsync failed" in str(ei.value)
    assert "partial transfer" in str(ei.value)


def test_rsync_timeout_exits_1(tmp_path: Path) -> None:
    """A 124 (timeout) rc -> exit 1."""
    config_dir, _ = _setup(tmp_path, section=_full_section(tmp_path))
    with pytest.raises(UpgradeCliError) as ei:
        sw.run(
            config_dir=config_dir,
            project_root=tmp_path,
            runner=_fake_runner_returning(124),
        )
    assert ei.value.exit_code == 1
    assert "timed out" in str(ei.value)


def test_rsync_binary_missing_exits_1(tmp_path: Path) -> None:
    """A 127 (binary not found) rc -> exit 1."""
    config_dir, _ = _setup(tmp_path, section=_full_section(tmp_path))
    with pytest.raises(UpgradeCliError) as ei:
        sw.run(
            config_dir=config_dir,
            project_root=tmp_path,
            runner=_fake_runner_returning(127, "rsync: command not found"),
        )
    assert ei.value.exit_code == 1
    assert "rsync not available" in str(ei.value)


def test_rsync_runner_raises_exits_1(tmp_path: Path) -> None:
    """A runner that raises -> exit 1."""
    config_dir, _ = _setup(tmp_path, section=_full_section(tmp_path))

    def _raising(argv: list[str], timeout: float) -> tuple[int, str, str]:
        del argv, timeout
        raise RuntimeError("simulated launch failure")

    with pytest.raises(UpgradeCliError) as ei:
        sw.run(config_dir=config_dir, project_root=tmp_path, runner=_raising)
    assert ei.value.exit_code == 1
    assert "rsync invocation raised" in str(ei.value)


def test_empty_snapshot_exits_1(tmp_path: Path) -> None:
    """Rsync succeeds but produces no files -> exit 1."""
    config_dir, _ = _setup(tmp_path, section=_full_section(tmp_path))
    # Runner returns 0 but writes nothing.
    with pytest.raises(UpgradeCliError) as ei:
        sw.run(
            config_dir=config_dir,
            project_root=tmp_path,
            runner=_fake_runner_returning(0),
        )
    assert ei.value.exit_code == 1
    assert "snapshot is empty" in str(ei.value)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_writes_manifest_and_prints_snapshot_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful snapshot -> manifest written, snapshot_id is a real SHA-256."""
    config_dir, _ = _setup(tmp_path, section=_full_section(tmp_path))
    files = {
        "level.dat": b"world-level",
        "region/r.0.0.mca": b"region-data",
        "DIM-1/data/foo.dat": b"dim1-data",
    }
    runner = _fake_rsync_success(files)

    code = sw.run(config_dir=config_dir, project_root=tmp_path, runner=runner)

    assert code == 0

    # Find the created snapshot directory
    snapshots_root = tmp_path / "snapshots"
    subdirs = list(snapshots_root.iterdir())
    assert len(subdirs) == 1
    snapshot_dir = subdirs[0]
    world_dir = snapshot_dir / "world"

    # Every source file landed
    for name, content in files.items():
        assert (world_dir / name).read_bytes() == content

    # Manifest is present, parses, and its file list is sorted + hashed
    manifest_path = snapshot_dir / "snapshot-manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest["schema_version"] == 1
    assert manifest["source"] == "/srv/minecraft/survival/world"
    paths = [e["path"] for e in manifest["files"]]
    assert paths == sorted(paths), "manifest entries must be sorted by path"
    for entry in manifest["files"]:
        actual = hashlib.sha256((world_dir / entry["path"]).read_bytes()).hexdigest()
        assert actual == entry["sha256"]

    # Snapshot ID matches sha256 of the manifest bytes
    expected_id = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    out = capsys.readouterr().out
    assert expected_id in out
    assert "snapshot_id:" in out


def test_happy_path_uses_default_world_when_none(
    tmp_path: Path,
) -> None:
    """Omitting ``world=`` uses ``[world_snapshot].default_world``."""
    config_dir, _ = _setup(
        tmp_path,
        section=_full_section_override(tmp_path, default_world="creative"),
    )
    runner = _fake_rsync_success({"level.dat": b"x"})

    code = sw.run(config_dir=config_dir, project_root=tmp_path, runner=runner)
    assert code == 0

    snapshots_root = tmp_path / "snapshots"
    subdirs = list(snapshots_root.iterdir())
    assert len(subdirs) == 1
    assert subdirs[0].name.startswith("creative-")
