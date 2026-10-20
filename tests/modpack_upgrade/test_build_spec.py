# tests/modpack_upgrade/test_build_spec.py

"""Unit tests for ``build.spec`` -- YAML loading and variable resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from minecraft.modpack_upgrade.build.spec import (
    BuildArtifact,
    BuildSpec,
    load_build_spec,
    resolve_variables,
)
from minecraft.modpack_upgrade.errors import SchemaError


def _write_spec(path: Path, body: dict) -> Path:
    """Write ``body`` as a YAML spec at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _minimal_body() -> dict:
    """Return the smallest valid spec body."""
    return {
        "repository": "https://example.com/repo.git",
        "ref": "v1.0.0",
        "entrypoint": "bash",
        "script": "build.sh",
        "artifacts": [{"path": "build/*.jar"}],
    }


# ---------------------------------------------------------------------------
# load_build_spec -- happy paths
# ---------------------------------------------------------------------------


def test_load_minimal_spec(tmp_path: Path) -> None:
    """A minimal spec parses with documented defaults."""
    path = _write_spec(tmp_path / "mod.yaml", _minimal_body())
    spec = load_build_spec(path)
    assert spec.name == "mod"
    assert spec.repository == "https://example.com/repo.git"
    assert spec.ref == "v1.0.0"
    assert spec.entrypoint == "bash"
    assert spec.script == "build.sh"
    assert spec.artifacts == (BuildArtifact(path="build/*.jar"),)
    assert spec.args == ()
    assert spec.env == {}
    assert spec.timeout_seconds == 1800.0
    assert spec.working_dir == "."
    assert spec.depth == 0


def test_load_full_spec(tmp_path: Path) -> None:
    """Every optional field is honoured."""
    body = {
        **_minimal_body(),
        "variables": {"mod_version": "1.0.0"},
        "args": ["--foo"],
        "env": {"JAVA_HOME": "/usr/lib/jvm/jdk-17"},
        "timeout_seconds": 600,
        "working_dir": "sub",
        "depth": 1,
        "artifacts": [
            {"path": "build/*.jar"},
            {"path": "build/.index/*.pw.toml", "dest": "sync/downloads/.index/special.pw.toml"},
        ],
    }
    path = _write_spec(tmp_path / "full.yaml", body)
    spec = load_build_spec(path)
    assert spec.variables == {"mod_version": "1.0.0"}
    assert spec.args == ("--foo",)
    assert spec.env == {"JAVA_HOME": "/usr/lib/jvm/jdk-17"}
    assert spec.timeout_seconds == 600.0
    assert spec.working_dir == "sub"
    assert spec.depth == 1
    assert len(spec.artifacts) == 2
    assert spec.artifacts[1].dest == "sync/downloads/.index/special.pw.toml"


def test_artifact_short_form(tmp_path: Path) -> None:
    """A string artifact entry becomes ``BuildArtifact(path=...)``."""
    body = {**_minimal_body(), "artifacts": ["build/*.jar"]}
    spec = load_build_spec(_write_spec(tmp_path / "m.yaml", body))
    assert spec.artifacts == (BuildArtifact(path="build/*.jar"),)


# ---------------------------------------------------------------------------
# load_build_spec -- failures
# ---------------------------------------------------------------------------


def test_load_missing_file_raises(tmp_path: Path) -> None:
    """A missing spec file raises SchemaError."""
    with pytest.raises(SchemaError, match="not found"):
        load_build_spec(tmp_path / "nope.yaml")


def test_load_malformed_yaml_raises(tmp_path: Path) -> None:
    """Malformed YAML raises SchemaError."""
    path = tmp_path / "bad.yaml"
    path.write_text(": invalid : yaml :\n", encoding="utf-8")
    with pytest.raises(SchemaError, match="could not read"):
        load_build_spec(path)


def test_load_non_mapping_top_level_raises(tmp_path: Path) -> None:
    """A list at the top level raises SchemaError."""
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(SchemaError, match="top-level must be a mapping"):
        load_build_spec(path)


@pytest.mark.parametrize("field", ["repository", "ref", "entrypoint", "script"])
def test_load_missing_required_field_raises(tmp_path: Path, field: str) -> None:
    """Each required field, when absent, raises SchemaError."""
    body = _minimal_body()
    del body[field]
    with pytest.raises(SchemaError, match=field):
        load_build_spec(_write_spec(tmp_path / "m.yaml", body))


def test_load_empty_required_field_raises(tmp_path: Path) -> None:
    """An empty-string required field raises SchemaError."""
    body = {**_minimal_body(), "repository": ""}
    with pytest.raises(SchemaError, match="repository"):
        load_build_spec(_write_spec(tmp_path / "m.yaml", body))


def test_load_empty_artifacts_raises(tmp_path: Path) -> None:
    """An empty artifacts list raises SchemaError."""
    body = {**_minimal_body(), "artifacts": []}
    with pytest.raises(SchemaError, match="artifacts"):
        load_build_spec(_write_spec(tmp_path / "m.yaml", body))


def test_load_negative_timeout_raises(tmp_path: Path) -> None:
    """A non-positive timeout raises SchemaError."""
    body = {**_minimal_body(), "timeout_seconds": 0}
    with pytest.raises(SchemaError, match="timeout_seconds"):
        load_build_spec(_write_spec(tmp_path / "m.yaml", body))


def test_load_negative_depth_raises(tmp_path: Path) -> None:
    """A negative depth raises SchemaError."""
    body = {**_minimal_body(), "depth": -1}
    with pytest.raises(SchemaError, match="depth"):
        load_build_spec(_write_spec(tmp_path / "m.yaml", body))


# ---------------------------------------------------------------------------
# resolve_variables
# ---------------------------------------------------------------------------


def _spec(**overrides: object) -> BuildSpec:
    """Return a minimal BuildSpec with optional field overrides."""
    base: dict[str, object] = {
        "name": "mod",
        "variables": {},
        "repository": "https://example.com/repo.git",
        "ref": "v1.0.0",
        "entrypoint": "bash",
        "script": "build.sh",
        "artifacts": (BuildArtifact(path="build/*.jar"),),
    }
    base.update(overrides)
    return BuildSpec(**base)  # type: ignore[arg-type]


def test_resolve_no_variables_is_noop() -> None:
    """A spec without ``${...}`` is returned unchanged."""
    original = _spec()
    resolved = resolve_variables(original, {}, {})
    assert resolved == original


def test_resolve_manifest_variable() -> None:
    """``${minecraft_version}`` is substituted from ``defaults``."""
    original = _spec(ref="mc${minecraft_version}")
    resolved = resolve_variables(original, {"minecraft_version": "1.20.1"}, {})
    assert resolved.ref == "mc1.20.1"


def test_resolve_yaml_variable_wins_over_defaults() -> None:
    """A YAML-local variable shadows the same key from ``defaults``."""
    original = _spec(
        variables={"mod_version": "0.1.0"},
        ref="v${mod_version}",
    )
    resolved = resolve_variables(original, {"mod_version": "9.9.9"}, {})
    assert resolved.ref == "v0.1.0"


def test_resolve_override_wins_over_yaml_variable() -> None:
    """``overrides`` (from CLI) wins over YAML variables."""
    original = _spec(
        variables={"mod_version": "0.1.0"},
        ref="v${mod_version}",
    )
    resolved = resolve_variables(original, {}, {"mod_version": "0.1.0-override"})
    assert resolved.ref == "v0.1.0-override"


def test_resolve_override_wins_over_defaults_and_yaml() -> None:
    """``overrides`` wins over both ``defaults`` and YAML."""
    original = _spec(
        variables={"tag": "from-yaml"},
        ref="${tag}",
    )
    resolved = resolve_variables(
        original,
        {"tag": "from-defaults"},
        {"tag": "from-override"},
    )
    assert resolved.ref == "from-override"


def test_resolve_undefined_variable_raises() -> None:
    """A ``${undefined}`` reference raises SchemaError."""
    original = _spec(ref="v${missing}")
    with pytest.raises(SchemaError, match="undefined variable \\$\\{missing\\}"):
        resolve_variables(original, {}, {})


def test_resolve_brace_syntax_preserved() -> None:
    """Literal ``{...}`` (not ``${...}``) is left untouched."""
    original = _spec(script="build-{date}.sh")
    resolved = resolve_variables(original, {}, {})
    assert resolved.script == "build-{date}.sh"


def test_resolve_applies_to_all_fields() -> None:
    """Every string field is subject to substitution."""
    original = _spec(
        repository="https://${host}/repo.git",
        ref="v${version}",
        script="${script_name}",
        args=("--${flag}",),
        env={"JAVA_HOME": "${java}"},
        artifacts=(BuildArtifact(path="build/${name}*.jar"),),
        working_dir="${workdir}",
    )
    defaults = {
        "host": "example.com",
        "version": "1.0.0",
        "script_name": "build.sh",
        "flag": "verbose",
        "java": "/jdk-17",
        "name": "mod",
        "workdir": "src",
    }
    resolved = resolve_variables(original, defaults, {})
    assert resolved.repository == "https://example.com/repo.git"
    assert resolved.ref == "v1.0.0"
    assert resolved.script == "build.sh"
    assert resolved.args == ("--verbose",)
    assert resolved.env == {"JAVA_HOME": "/jdk-17"}
    assert resolved.artifacts[0].path == "build/mod*.jar"
    assert resolved.working_dir == "src"
