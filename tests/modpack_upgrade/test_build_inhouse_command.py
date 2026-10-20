# tests/modpack_upgrade/test_build_inhouse_command.py

"""Unit tests for ``cli.commands.build_inhouse`` -- the CLI dispatcher.

``run_build`` is monkeypatched so these tests focus on spec loading,
variable resolution, outcome emission, and CLI error codes without
requiring a real git repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from minecraft.modpack_upgrade.build.runner import BuildOutcome
from minecraft.modpack_upgrade.build.spec import BuildSpec
from minecraft.modpack_upgrade.cli.commands import build_inhouse as bi
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.cli.main import main

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_spec(config_dir: Path, name: str, body: dict) -> None:
    """Write a build spec at ``<config_dir>/build/<name>.yaml``."""
    path = config_dir / "build" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body), encoding="utf-8")


def _minimal_spec_body() -> dict:
    """Return a minimal valid spec body."""
    return {
        "side": "both",
        "repository": "https://example.com/repo.git",
        "ref": "v1.0.0",
        "entrypoint": "bash",
        "script": "build.sh",
        "artifacts": ["build/libs/*.jar"],
    }


def _project(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(config_dir, project_root)`` under ``tmp_path``."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    return (config_dir, tmp_path)


def _success_outcome(name: str = "mod", staged: dict[str, Path] | None = None) -> BuildOutcome:
    """Return a successful BuildOutcome with optional staged artifacts."""
    return BuildOutcome(
        success=True,
        name=name,
        clone_dir=Path("/tmp/unused"),
        staged_artifacts=staged
        or {
            "/tmp/unused/mod-1.0.0.jar": Path("/tmp/unused/mod-1.0.0.jar"),
            "/tmp/unused/.index/mod-1.0.0.pw.toml": Path("/tmp/unused/.index/mod-1.0.0.pw.toml"),
        },
    )


def _failure_outcome(name: str = "mod", message: str = "boom") -> BuildOutcome:
    """Return a failed BuildOutcome."""
    return BuildOutcome(
        success=False,
        name=name,
        clone_dir=Path("/tmp/unused"),
        failure_message=message,
    )


# ---------------------------------------------------------------------------
# Spec loading failures
# ---------------------------------------------------------------------------


def test_missing_spec_raises_exit_3(tmp_path: Path) -> None:
    """A missing spec file yields UpgradeCliError(3)."""
    config_dir, project_root = _project(tmp_path)
    with pytest.raises(UpgradeCliError) as ei:
        bi.run(config_dir=config_dir, project_root=project_root, name="missing")
    assert ei.value.exit_code == 3
    assert "build spec error" in str(ei.value)


def test_missing_side_raises_exit_3(tmp_path: Path) -> None:
    """A spec without ``side`` yields UpgradeCliError(3)."""
    config_dir, project_root = _project(tmp_path)
    body = _minimal_spec_body()
    del body["side"]
    _write_spec(config_dir, "mod", body)
    with pytest.raises(UpgradeCliError) as ei:
        bi.run(config_dir=config_dir, project_root=project_root, name="mod")
    assert ei.value.exit_code == 3
    assert "build spec error" in str(ei.value)


def test_invalid_spec_raises_exit_3(tmp_path: Path) -> None:
    """A malformed YAML spec yields UpgradeCliError(3)."""
    config_dir, project_root = _project(tmp_path)
    path = config_dir / "build" / "bad.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(": invalid : yaml :\n", encoding="utf-8")
    with pytest.raises(UpgradeCliError) as ei:
        bi.run(config_dir=config_dir, project_root=project_root, name="bad")
    assert ei.value.exit_code == 3
    assert "build spec error" in str(ei.value)


# ---------------------------------------------------------------------------
# Happy path and build failure
# ---------------------------------------------------------------------------


def test_happy_path_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful build returns 0."""
    config_dir, project_root = _project(tmp_path)
    _write_spec(config_dir, "mod", _minimal_spec_body())
    monkeypatch.setattr(bi, "run_build", lambda spec, **_kw: _success_outcome())
    assert bi.run(config_dir=config_dir, project_root=project_root, name="mod") == 0


def test_build_failure_raises_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed build raises UpgradeCliError(1) with the diagnostic."""
    config_dir, project_root = _project(tmp_path)
    _write_spec(config_dir, "mod", _minimal_spec_body())
    monkeypatch.setattr(bi, "run_build", lambda spec, **_kw: _failure_outcome(message="gradle blew up"))
    with pytest.raises(UpgradeCliError) as ei:
        bi.run(config_dir=config_dir, project_root=project_root, name="mod")
    assert ei.value.exit_code == 1
    assert "gradle blew up" in str(ei.value)


# ---------------------------------------------------------------------------
# Variable resolution
# ---------------------------------------------------------------------------


def test_manifest_variables_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``${minecraft_version}`` is substituted from the manifest."""
    config_dir, project_root = _project(tmp_path)
    body = {**_minimal_spec_body(), "ref": "mc${minecraft_version}"}
    _write_spec(config_dir, "mod", body)
    manifest = project_root / "sync" / "modpack.manifest.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '[minecraft]\nversion = "1.20.1"\nloader = "forge"\nloader_version = "47.4.10"\n[policy]\nselection = "release_then_beta"\nno_version = "ask"\n',
        encoding="utf-8",
    )
    captured: dict[str, BuildSpec] = {}

    def _capture(spec: BuildSpec, **_kw: object) -> BuildOutcome:
        captured["spec"] = spec
        return _success_outcome()

    monkeypatch.setattr(bi, "run_build", _capture)
    bi.run(config_dir=config_dir, project_root=project_root, name="mod")
    assert captured["spec"].ref == "mc1.20.1"


def test_override_variables_win(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller-supplied overrides win over YAML-local variables."""
    config_dir, project_root = _project(tmp_path)
    body = {**_minimal_spec_body(), "variables": {"tag": "default"}, "ref": "${tag}"}
    _write_spec(config_dir, "mod", body)
    captured: dict[str, BuildSpec] = {}

    def _capture(spec: BuildSpec, **_kw: object) -> BuildOutcome:
        captured["spec"] = spec
        return _success_outcome()

    monkeypatch.setattr(bi, "run_build", _capture)
    bi.run(config_dir=config_dir, project_root=project_root, name="mod", overrides={"tag": "v9.9.9"})
    assert captured["spec"].ref == "v9.9.9"


def test_undefined_variable_raises_exit_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An undefined ``${VAR}`` after resolution raises UpgradeCliError(3)."""
    config_dir, project_root = _project(tmp_path)
    body = {**_minimal_spec_body(), "ref": "v${undefined_var}"}
    _write_spec(config_dir, "mod", body)
    monkeypatch.setattr(bi, "run_build", lambda spec, **_kw: _success_outcome())
    with pytest.raises(UpgradeCliError) as ei:
        bi.run(config_dir=config_dir, project_root=project_root, name="mod")
    assert ei.value.exit_code == 3
    assert "variable resolution error" in str(ei.value)


# ---------------------------------------------------------------------------
# CLI parser integration
# ---------------------------------------------------------------------------


def test_main_dispatches_build_inhouse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``main(['build-inhouse', 'mod'])`` reaches the command handler."""
    config_dir, _project_root = _project(tmp_path)
    _write_spec(config_dir, "mod", _minimal_spec_body())
    called: list[BuildSpec] = []

    def _capture(spec: BuildSpec, **_kw: object) -> BuildOutcome:
        called.append(spec)
        return _success_outcome()

    monkeypatch.setattr(bi, "run_build", _capture)
    code = main(["--config-dir", str(config_dir), "build-inhouse", "mod"])
    assert code == 0
    assert len(called) == 1
    assert called[0].side == "both"


def test_main_var_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--var key=value`` overrides reach the resolved spec."""
    config_dir, _project_root = _project(tmp_path)
    body = {**_minimal_spec_body(), "variables": {"tag": "default"}, "ref": "${tag}"}
    _write_spec(config_dir, "mod", body)
    captured: dict[str, BuildSpec] = {}

    def _capture(spec: BuildSpec, **_kw: object) -> BuildOutcome:
        captured["spec"] = spec
        return _success_outcome()

    monkeypatch.setattr(bi, "run_build", _capture)
    code = main(["--config-dir", str(config_dir), "build-inhouse", "mod", "--var", "tag=v0.5.0"])
    assert code == 0
    assert captured["spec"].ref == "v0.5.0"


def test_main_invalid_var_format(tmp_path: Path) -> None:
    """A ``--var`` value without '=' produces exit 2."""
    config_dir, _project_root = _project(tmp_path)
    _write_spec(config_dir, "mod", _minimal_spec_body())
    code = main(["--config-dir", str(config_dir), "build-inhouse", "mod", "--var", "no-equals-sign"])
    assert code == 2


def test_main_legacy_revision_warns_but_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second positional argument is warned about and ignored."""
    config_dir, _project_root = _project(tmp_path)
    _write_spec(config_dir, "mod", _minimal_spec_body())
    monkeypatch.setattr(bi, "run_build", lambda spec, **_kw: _success_outcome())
    code = main(["--config-dir", str(config_dir), "build-inhouse", "mod", "abc123"])
    assert code == 0
