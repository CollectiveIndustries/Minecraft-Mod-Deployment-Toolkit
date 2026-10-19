# tests/modpack_upgrade/test_compose_reconcile.py

r"""Unit tests for compose reconciliation (Slice 9 Turns 3 and 7).

Covers the four new pieces of ``cli.commands._compose``:

  * :class:`ComposeRuntime`, :class:`EnvChange`, :class:`ComposeDiff`
  * :func:`reconcile_compose` - read-only diff computation
  * :func:`apply_compose_diff` - atomic write with verify-before-write

Turn 3 exercised the happy paths and the reconcile-side failure modes.
Turn 7 adds the ``apply_compose_diff`` failure paths: every ``try`` /
``except`` in the writer has a targeted test, so a future refactor
cannot silently change the diagnostic an operator sees when pre-commit
verification fails.

Note on the "malformed YAML" fixtures: ``yaml.safe_load`` returns a
scalar string for many payloads that *look* malformed (e.g.
``":::not yaml"`` yields the string ``":::not yaml"``). Payloads that
must fail at the *parse* step use an unterminated flow sequence
(``"key: [unclosed\\n"``), which raises :class:`yaml.YAMLError`.
Payloads that must fail at the *semantic* step (non-mapping top-level,
missing services table) are tested separately in
:func:`test_reconcile_*` above and covered by
``test_apply_needed_but_service_vanished_raises`` below.

The module uses ``yaml`` (PyYAML) directly rather than ``load_compose``
because reconciliation must preserve arbitrary structure, not just the
fields ``load_compose`` models.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from minecraft.modpack_upgrade.cli.commands import _compose
from minecraft.modpack_upgrade.cli.commands._compose import (
    ComposeDiff,
    ComposeRuntime,
    EnvChange,
    apply_compose_diff,
    reconcile_compose,
)
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _runtime(
    *,
    mc: str = "1.21.1",
    loader: str = "forge",
    loader_version: str = "52.0.1",
) -> ComposeRuntime:
    """Build a ``ComposeRuntime`` fixture."""
    return ComposeRuntime(
        minecraft_version=mc,
        loader=loader,
        loader_version=loader_version,
    )


def _setup(tmp_path: Path, compose_body: str) -> tuple[Path, Path]:
    """Create ``<tmp>/config.d/`` and ``<tmp>/docker-compose.yml``.

    Returns ``(config_dir, project_root)``.
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (tmp_path / "docker-compose.yml").write_text(compose_body, encoding="utf-8")
    return config_dir, tmp_path


_FORGE_COMPOSE = """\
services:
  mc-survival:
    image: itzg/minecraft-server
    environment:
      VERSION: "1.21.1"
      TYPE: "FORGE"
      FORGE_VERSION: "52.0.1"
"""


# ---------------------------------------------------------------------------
# Dataclass basics
# ---------------------------------------------------------------------------


def test_compose_runtime_construction() -> None:
    """ComposeRuntime carries the three runtime fields."""
    rt = _runtime(mc="1.21.1", loader="forge", loader_version="52.0.1")
    assert rt.minecraft_version == "1.21.1"
    assert rt.loader == "forge"
    assert rt.loader_version == "52.0.1"


def test_env_change_update_encodes_before_after() -> None:
    """EnvChange with both sides set represents an update."""
    change = EnvChange(key="VERSION", old_value="1.21.1", new_value="1.21.2")
    assert change.key == "VERSION"
    assert change.old_value == "1.21.1"
    assert change.new_value == "1.21.2"


def test_env_change_add_and_remove() -> None:
    """EnvChange with one None side represents an add or a removal."""
    add = EnvChange(key="NEOFORGE_VERSION", old_value=None, new_value="21.0.1")
    remove = EnvChange(key="FORGE_VERSION", old_value="52.0.1", new_value=None)
    assert add.old_value is None
    assert remove.new_value is None


def test_compose_diff_needed_true() -> None:
    """ComposeDiff.needed is True when changes are present."""
    diff = ComposeDiff(
        compose_path=Path("/tmp/dc.yml"),
        target=_runtime(),
        current=_runtime(),
        changes=(EnvChange(key="VERSION", old_value="1.21.0", new_value="1.21.1"),),
    )
    assert diff.needed is True


def test_compose_diff_needed_false() -> None:
    """ComposeDiff.needed is False when changes are absent."""
    diff = ComposeDiff(
        compose_path=Path("/tmp/dc.yml"),
        target=_runtime(),
        current=_runtime(),
        changes=(),
    )
    assert diff.needed is False


# ---------------------------------------------------------------------------
# reconcile_compose - happy paths
# ---------------------------------------------------------------------------


def test_reconcile_no_op_when_matches(tmp_path: Path) -> None:
    """A compose file already matching the target yields an empty diff."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    diff = reconcile_compose(config_dir, project_root, _runtime())
    assert diff.needed is False
    assert diff.changes == ()
    assert diff.current == _runtime()


def test_reconcile_version_only_change(tmp_path: Path) -> None:
    """Only VERSION differs -> a single update change."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    diff = reconcile_compose(config_dir, project_root, _runtime(mc="1.21.2"))
    assert diff.needed is True
    assert len(diff.changes) == 1
    assert diff.changes[0] == EnvChange(key="VERSION", old_value="1.21.1", new_value="1.21.2")


def test_reconcile_all_three_change(tmp_path: Path) -> None:
    """Different mc, loader, and loader-version all appear as updates."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    target = _runtime(mc="1.21.2", loader="neoforge", loader_version="21.0.1")
    diff = reconcile_compose(config_dir, project_root, target)
    # Version update + TYPE update + NEOFORGE_VERSION add + FORGE_VERSION remove
    assert diff.needed is True
    by_key = {c.key: c for c in diff.changes}
    assert by_key["VERSION"] == EnvChange(key="VERSION", old_value="1.21.1", new_value="1.21.2")
    assert by_key["TYPE"] == EnvChange(key="TYPE", old_value="FORGE", new_value="NEOFORGE")
    assert by_key["NEOFORGE_VERSION"] == EnvChange(key="NEOFORGE_VERSION", old_value=None, new_value="21.0.1")
    assert by_key["FORGE_VERSION"] == EnvChange(key="FORGE_VERSION", old_value="52.0.1", new_value=None)


def test_reconcile_loader_change_emits_stale_key_removal(tmp_path: Path) -> None:
    """Switching FORGE -> NEOFORGE emits a removal for FORGE_VERSION."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    diff = reconcile_compose(config_dir, project_root, _runtime(loader="neoforge", loader_version="21.0.1"))
    removals = [c for c in diff.changes if c.new_value is None]
    assert len(removals) == 1
    assert removals[0].key == "FORGE_VERSION"
    assert removals[0].old_value == "52.0.1"


def test_reconcile_alternate_mc_key_preserved(tmp_path: Path) -> None:
    """If the file uses MINECRAFT_VERSION, the update keeps that key."""
    compose = """\
services:
  mc-survival:
    image: itzg/minecraft-server
    environment:
      MINECRAFT_VERSION: "1.21.1"
      TYPE: "FORGE"
      FORGE_VERSION: "52.0.1"
"""
    config_dir, project_root = _setup(tmp_path, compose)
    diff = reconcile_compose(config_dir, project_root, _runtime(mc="1.21.2"))
    assert diff.needed is True
    assert diff.changes[0].key == "MINECRAFT_VERSION"
    assert diff.changes[0].new_value == "1.21.2"


def test_reconcile_missing_version_emits_addition(tmp_path: Path) -> None:
    """A missing VERSION key is emitted as an addition under the primary key."""
    compose = """\
services:
  mc-survival:
    image: itzg/minecraft-server
    environment:
      TYPE: "FORGE"
      FORGE_VERSION: "52.0.1"
"""
    config_dir, project_root = _setup(tmp_path, compose)
    diff = reconcile_compose(config_dir, project_root, _runtime())
    additions = [c for c in diff.changes if c.old_value is None and c.new_value is not None]
    assert len(additions) == 1
    assert additions[0].key == "VERSION"
    assert additions[0].new_value == "1.21.1"


def test_reconcile_list_form_env_normalized(tmp_path: Path) -> None:
    """A list-form environment produces the same diff shape as dict form."""
    compose = """\
services:
  mc-survival:
    image: itzg/minecraft-server
    environment:
      - VERSION=1.21.1
      - TYPE=FORGE
      - FORGE_VERSION=52.0.1
"""
    config_dir, project_root = _setup(tmp_path, compose)
    diff = reconcile_compose(config_dir, project_root, _runtime(mc="1.21.2"))
    assert diff.needed is True
    assert len(diff.changes) == 1
    assert diff.changes[0] == EnvChange(key="VERSION", old_value="1.21.1", new_value="1.21.2")


# ---------------------------------------------------------------------------
# reconcile_compose - failure paths
# ---------------------------------------------------------------------------


def test_reconcile_missing_compose_file_raises(tmp_path: Path) -> None:
    """Missing compose file -> exit 3."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    with pytest.raises(UpgradeCliError) as ei:
        reconcile_compose(config_dir, tmp_path, _runtime())
    assert ei.value.exit_code == 3
    assert "compose file not found" in str(ei.value)


def test_reconcile_unreadable_compose_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError during read -> exit 3."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    real_read = Path.read_bytes

    def _fail(self: Path) -> bytes:
        if self.name == "docker-compose.yml":
            raise OSError("simulated read failure")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", _fail)
    with pytest.raises(UpgradeCliError) as ei:
        reconcile_compose(config_dir, project_root, _runtime())
    assert ei.value.exit_code == 3
    assert "could not read" in str(ei.value)


def test_reconcile_malformed_yaml_raises(tmp_path: Path) -> None:
    """Malformed YAML -> exit 3."""
    config_dir, project_root = _setup(tmp_path, "key: [unclosed\n")
    with pytest.raises(UpgradeCliError) as ei:
        reconcile_compose(config_dir, project_root, _runtime())
    assert ei.value.exit_code == 3
    assert "not valid YAML" in str(ei.value)


def test_reconcile_no_minecraft_service_raises(tmp_path: Path) -> None:
    """A compose file with no minecraft service -> exit 3."""
    compose = """\
services:
  web:
    image: nginx:latest
"""
    config_dir, project_root = _setup(tmp_path, compose)
    with pytest.raises(UpgradeCliError) as ei:
        reconcile_compose(config_dir, project_root, _runtime())
    assert ei.value.exit_code == 3
    assert "no service declares an image containing 'minecraft'" in str(ei.value)


def test_reconcile_multiple_minecraft_services_raises(tmp_path: Path) -> None:
    """Two minecraft services -> exit 3 (ambiguous)."""
    compose = """\
services:
  mc-a:
    image: itzg/minecraft-server
    environment:
      VERSION: "1.21.1"
      TYPE: "FORGE"
      FORGE_VERSION: "52.0.1"
  mc-b:
    image: itzg/minecraft-server
    environment:
      VERSION: "1.21.1"
      TYPE: "FORGE"
      FORGE_VERSION: "52.0.1"
"""
    config_dir, project_root = _setup(tmp_path, compose)
    with pytest.raises(UpgradeCliError) as ei:
        reconcile_compose(config_dir, project_root, _runtime())
    assert ei.value.exit_code == 3
    assert "2 services declare minecraft images" in str(ei.value)


def test_reconcile_unknown_target_loader_raises(tmp_path: Path) -> None:
    """An unrecognised target loader -> exit 3 (defensive)."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    bad_target = ComposeRuntime(minecraft_version="1.21.1", loader="bogus", loader_version="1.0")
    with pytest.raises(UpgradeCliError) as ei:
        reconcile_compose(config_dir, project_root, bad_target)
    assert ei.value.exit_code == 3
    assert "unknown target loader" in str(ei.value)


# ---------------------------------------------------------------------------
# apply_compose_diff - happy paths
# ---------------------------------------------------------------------------


def test_apply_no_op_returns_current_bytes(tmp_path: Path) -> None:
    """An empty diff returns the current bytes without modifying the file."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    diff = reconcile_compose(config_dir, project_root, _runtime())
    assert diff.needed is False

    original = (project_root / "docker-compose.yml").read_bytes()
    result = apply_compose_diff(diff)

    assert result == original
    assert (project_root / "docker-compose.yml").read_bytes() == original


def test_apply_writes_and_round_trips(tmp_path: Path) -> None:
    """After applying a diff, reconcile-again finds nothing to do."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    target = _runtime(mc="1.21.2")
    diff = reconcile_compose(config_dir, project_root, target)
    assert diff.needed is True

    result = apply_compose_diff(diff)

    # Returned bytes match the on-disk file.
    assert result == (project_root / "docker-compose.yml").read_bytes()

    # Re-reconciling yields an empty diff.
    diff2 = reconcile_compose(config_dir, project_root, target)
    assert diff2.needed is False
    assert diff2.current is not None
    assert diff2.current.minecraft_version == "1.21.2"


def test_apply_is_idempotent(tmp_path: Path) -> None:
    """Applying the same diff twice yields identical bytes."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    target = _runtime(mc="1.21.2")
    diff = reconcile_compose(config_dir, project_root, target)
    apply_compose_diff(diff)
    first_bytes = (project_root / "docker-compose.yml").read_bytes()

    # Applying the same diff again would overwrite with the same content;
    # but since the file already matches, a re-reconcile yields empty.
    diff2 = reconcile_compose(config_dir, project_root, target)
    assert diff2.needed is False
    apply_compose_diff(diff2)

    assert (project_root / "docker-compose.yml").read_bytes() == first_bytes


def test_apply_loader_swap_removes_stale_key(tmp_path: Path) -> None:
    """Applying a loader swap removes the old loader-version key."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    target = _runtime(loader="neoforge", loader_version="21.0.1")
    diff = reconcile_compose(config_dir, project_root, target)
    apply_compose_diff(diff)

    on_disk = yaml.safe_load((project_root / "docker-compose.yml").read_bytes())
    env = on_disk["services"]["mc-survival"]["environment"]
    assert env["TYPE"] == "NEOFORGE"
    assert env["NEOFORGE_VERSION"] == "21.0.1"
    assert "FORGE_VERSION" not in env


def test_apply_list_form_env_rewritten_as_dict(tmp_path: Path) -> None:
    """A list-form env is written back as a dict after reconciliation."""
    compose = """\
services:
  mc-survival:
    image: itzg/minecraft-server
    environment:
      - VERSION=1.21.1
      - TYPE=FORGE
      - FORGE_VERSION=52.0.1
"""
    config_dir, project_root = _setup(tmp_path, compose)
    target = _runtime(mc="1.21.2")
    diff = reconcile_compose(config_dir, project_root, target)
    apply_compose_diff(diff)

    on_disk = yaml.safe_load((project_root / "docker-compose.yml").read_bytes())
    env = on_disk["services"]["mc-survival"]["environment"]
    assert isinstance(env, dict)
    assert env["VERSION"] == "1.21.2"


def test_apply_preserves_unrelated_services(tmp_path: Path) -> None:
    """Reconciliation touches only the minecraft service's env block."""
    compose = """\
services:
  mc-survival:
    image: itzg/minecraft-server
    environment:
      VERSION: "1.21.1"
      TYPE: "FORGE"
      FORGE_VERSION: "52.0.1"
  web:
    image: nginx:latest
    ports:
      - "80:80"
"""
    config_dir, project_root = _setup(tmp_path, compose)
    target = _runtime(mc="1.21.2")
    diff = reconcile_compose(config_dir, project_root, target)
    apply_compose_diff(diff)

    on_disk = yaml.safe_load((project_root / "docker-compose.yml").read_bytes())
    # web service untouched.
    assert on_disk["services"]["web"]["image"] == "nginx:latest"
    assert on_disk["services"]["web"]["ports"] == ["80:80"]
    # minecraft service updated.
    assert on_disk["services"]["mc-survival"]["environment"]["VERSION"] == "1.21.2"


def test_reconcile_apply_reconcile_cycle_is_stable(tmp_path: Path) -> None:
    """Reconcile -> apply -> reconcile yields an empty diff at every subsequent cycle."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    target = _runtime(mc="1.21.2", loader="neoforge", loader_version="21.0.1")

    first = reconcile_compose(config_dir, project_root, target)
    assert first.needed is True
    apply_compose_diff(first)

    second = reconcile_compose(config_dir, project_root, target)
    assert second.needed is False
    apply_compose_diff(second)

    third = reconcile_compose(config_dir, project_root, target)
    assert third.needed is False


# ---------------------------------------------------------------------------
# apply_compose_diff - failure paths (Slice 9 Turn 7)
# ---------------------------------------------------------------------------
#
# Every try/except in apply_compose_diff has a targeted test. The
# "verify-*" cases construct ComposeDiff/EnvChange by hand rather than
# via reconcile_compose, because a real reconcile never produces a pure
# removal-only diff (removals always accompany the TYPE swap, so the
# TYPE update check fires first). Building the diff manually exercises
# the frozen dataclasses and pins the exact verify order.


def test_apply_noop_but_file_disappeared_raises(tmp_path: Path) -> None:
    """A no-op diff whose target file was deleted between reconcile and apply -> exit 3."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    diff = reconcile_compose(config_dir, project_root, _runtime())
    assert diff.needed is False
    (project_root / "docker-compose.yml").unlink()

    with pytest.raises(UpgradeCliError) as ei:
        apply_compose_diff(diff)
    assert ei.value.exit_code == 3
    assert "disappeared between reconcile and apply" in str(ei.value)


def test_apply_needed_but_file_disappeared_raises(tmp_path: Path) -> None:
    """A needed diff whose target file was deleted -> exit 3 with read diagnostic."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    diff = reconcile_compose(config_dir, project_root, _runtime(mc="1.21.2"))
    assert diff.needed is True
    (project_root / "docker-compose.yml").unlink()

    with pytest.raises(UpgradeCliError) as ei:
        apply_compose_diff(diff)
    assert ei.value.exit_code == 3
    assert "could not read" in str(ei.value)


def test_apply_needed_but_file_became_malformed_yaml_raises(tmp_path: Path) -> None:
    """A needed diff whose file was corrupted to invalid YAML between calls -> exit 3.

    The corruption must actually fail ``yaml.safe_load``. Payloads like
    ``":::not yaml"`` parse cleanly as a plain scalar and land on the
    "top-level is not a mapping" guard instead; an unterminated flow
    sequence is used here to reach the parse-error branch this test
    targets.
    """
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    diff = reconcile_compose(config_dir, project_root, _runtime(mc="1.21.2"))
    assert diff.needed is True
    (project_root / "docker-compose.yml").write_text("key: [unclosed\n", encoding="utf-8")

    with pytest.raises(UpgradeCliError) as ei:
        apply_compose_diff(diff)
    assert ei.value.exit_code == 3
    assert "not valid YAML" in str(ei.value)


def test_apply_needed_but_service_vanished_raises(tmp_path: Path) -> None:
    """A needed diff whose file no longer declares a minecraft service -> exit 3."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    diff = reconcile_compose(config_dir, project_root, _runtime(mc="1.21.2"))
    assert diff.needed is True
    (project_root / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx:latest\n", encoding="utf-8")

    with pytest.raises(UpgradeCliError) as ei:
        apply_compose_diff(diff)
    assert ei.value.exit_code == 3
    assert "no service declares an image containing 'minecraft'" in str(ei.value)


def test_apply_serialize_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``yaml.safe_dump`` failure -> exit 3 with serialize diagnostic."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    diff = reconcile_compose(config_dir, project_root, _runtime(mc="1.21.2"))
    assert diff.needed is True

    def _raise(*args: object, **kwargs: object) -> str:
        raise yaml.YAMLError("simulated dump failure")

    monkeypatch.setattr(_compose.yaml, "safe_dump", _raise)
    with pytest.raises(UpgradeCliError) as ei:
        apply_compose_diff(diff)
    assert ei.value.exit_code == 3
    assert "could not serialize" in str(ei.value)


def test_apply_verify_reparse_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The verify pass's ``yaml.safe_load`` failing on the serialized output -> exit 3.

    ``apply_compose_diff`` calls ``yaml.safe_load`` exactly twice: once
    for the original, once for the serialized output. Patching to raise
    on the second call exercises the re-parse branch.
    """
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    diff = reconcile_compose(config_dir, project_root, _runtime(mc="1.21.2"))
    assert diff.needed is True

    real_safe_load = _compose.yaml.safe_load
    call_count = [0]

    def _patched(payload: object) -> object:
        call_count[0] += 1
        if call_count[0] >= 2:
            raise yaml.YAMLError("simulated verify reparse failure")
        return real_safe_load(payload)

    monkeypatch.setattr(_compose.yaml, "safe_load", _patched)
    with pytest.raises(UpgradeCliError) as ei:
        apply_compose_diff(diff)
    assert ei.value.exit_code == 3
    assert "failed to re-parse" in str(ei.value)


def test_apply_verify_remove_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A removal that survives serialization triggers the verify-remove branch.

    A real reconcile pairs every removal with the TYPE update (loader
    swap), so the TYPE check would fire first. This test constructs a
    diff with only a removal, then monkeypatches ``safe_dump`` to return
    the original bytes unchanged (i.e., pretend the serializer did not
    drop the key). Verify then sees the key still present and raises.
    """
    _, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    compose_path = project_root / "docker-compose.yml"
    diff = ComposeDiff(
        compose_path=compose_path,
        target=_runtime(),
        current=_runtime(),
        changes=(EnvChange(key="FORGE_VERSION", old_value="52.0.1", new_value=None),),
    )
    assert diff.needed is True

    original_text = compose_path.read_text(encoding="utf-8")
    monkeypatch.setattr(_compose.yaml, "safe_dump", lambda *a, **kw: original_text)

    with pytest.raises(UpgradeCliError) as ei:
        apply_compose_diff(diff)
    assert ei.value.exit_code == 3
    assert "failed to remove" in str(ei.value)
    assert "FORGE_VERSION" in str(ei.value)


def test_apply_verify_update_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An update that survives serialization triggers the verify-update branch.

    Mirrors ``test_apply_verify_remove_failure_raises`` but for an
    update: ``safe_dump`` returns the original bytes unchanged, so
    verify sees the old value and raises with the expected/got
    diagnostic.
    """
    _, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    compose_path = project_root / "docker-compose.yml"
    diff = ComposeDiff(
        compose_path=compose_path,
        target=_runtime(),
        current=_runtime(),
        changes=(EnvChange(key="VERSION", old_value="1.21.1", new_value="1.21.2"),),
    )
    assert diff.needed is True

    original_text = compose_path.read_text(encoding="utf-8")
    monkeypatch.setattr(_compose.yaml, "safe_dump", lambda *a, **kw: original_text)

    with pytest.raises(UpgradeCliError) as ei:
        apply_compose_diff(diff)
    assert ei.value.exit_code == 3
    assert "failed for 'VERSION'" in str(ei.value)
    assert "expected '1.21.2'" in str(ei.value)
    assert "got '1.21.1'" in str(ei.value)


def test_apply_temp_write_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure writing the temp file -> exit 3; compose file is untouched."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    diff = reconcile_compose(config_dir, project_root, _runtime(mc="1.21.2"))
    assert diff.needed is True
    original_bytes = (project_root / "docker-compose.yml").read_bytes()

    real_write_bytes = Path.write_bytes

    def _patched(self: Path, data: bytes) -> int:
        if self.suffix == ".tmp":
            raise OSError("simulated temp write failure")
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", _patched)
    with pytest.raises(UpgradeCliError) as ei:
        apply_compose_diff(diff)
    assert ei.value.exit_code == 3
    assert "could not write temp compose file" in str(ei.value)
    # Compose file was not touched.
    assert (project_root / "docker-compose.yml").read_bytes() == original_bytes


def test_apply_replace_failure_raises_and_cleans_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure in ``os.replace`` -> exit 3, temp file cleaned up, compose untouched."""
    config_dir, project_root = _setup(tmp_path, _FORGE_COMPOSE)
    diff = reconcile_compose(config_dir, project_root, _runtime(mc="1.21.2"))
    assert diff.needed is True
    original_bytes = (project_root / "docker-compose.yml").read_bytes()

    real_replace = Path.replace

    def _patched(self: Path, target: Path) -> Path:
        if self.suffix == ".tmp":
            raise OSError("simulated replace failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _patched)
    with pytest.raises(UpgradeCliError) as ei:
        apply_compose_diff(diff)
    assert ei.value.exit_code == 3
    assert "could not replace compose file" in str(ei.value)
    # Temp file cleaned up by the suppress(OSError) block.
    tmp_file = project_root / "docker-compose.yml.tmp"
    assert not tmp_file.exists()
    # Compose file untouched (replace failed before the rename).
    assert (project_root / "docker-compose.yml").read_bytes() == original_bytes
