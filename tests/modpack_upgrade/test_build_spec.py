# tests/modpack_upgrade/test_build_spec.py

"""Unit tests for ``build.spec`` -- YAML loading and variable resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from minecraft.modpack_upgrade.build.spec import (
    BuildArtifact,
    BuildSpec,
    PwTomlDependency,
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
        "side": "both",
        "repository": "https://example.com/repo.git",
        "ref": "v1.0.0",
        "entrypoint": "bash",
        "script": "build.sh",
        "artifacts": ["build/libs/*.jar"],
    }


# ---------------------------------------------------------------------------
# load_build_spec -- happy paths
# ---------------------------------------------------------------------------


def test_load_minimal_spec(tmp_path: Path) -> None:
    """A minimal spec parses with documented defaults."""
    spec = load_build_spec(_write_spec(tmp_path / "mod.yaml", _minimal_body()))
    assert spec.name == "mod"
    assert spec.side == "both"
    assert spec.repository == "https://example.com/repo.git"
    assert spec.ref == "v1.0.0"
    assert spec.entrypoint == "bash"
    assert spec.script == "build.sh"
    assert spec.artifacts == (BuildArtifact(path="build/libs/*.jar"),)
    assert spec.mod_name is None
    assert spec.dependencies == ()
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
        "name": "Custom Mod",
        "dependencies": [{"addon_id": "flywheel", "type": "required"}],
        "args": ["--foo"],
        "env": {"JAVA_HOME": "/usr/lib/jvm/jdk-17"},
        "timeout_seconds": 600,
        "working_dir": "sub",
        "depth": 1,
    }
    spec = load_build_spec(_write_spec(tmp_path / "full.yaml", body))
    assert spec.variables == {"mod_version": "1.0.0"}
    assert spec.mod_name == "Custom Mod"
    assert spec.dependencies == (PwTomlDependency(addon_id="flywheel", type="required"),)
    assert spec.args == ("--foo",)
    assert spec.env == {"JAVA_HOME": "/usr/lib/jvm/jdk-17"}
    assert spec.timeout_seconds == 600.0
    assert spec.working_dir == "sub"
    assert spec.depth == 1


def test_load_multiple_artifacts(tmp_path: Path) -> None:
    """Multiple artifact patterns are preserved."""
    body = {**_minimal_body(), "artifacts": ["build/a/*.jar", "build/b/*.jar"]}
    spec = load_build_spec(_write_spec(tmp_path / "m.yaml", body))
    assert spec.artifacts == (BuildArtifact(path="build/a/*.jar"), BuildArtifact(path="build/b/*.jar"))


@pytest.mark.parametrize("side", ["client", "server", "both"])
def test_load_all_valid_sides(tmp_path: Path, side: str) -> None:
    """Every valid side is accepted."""
    body = {**_minimal_body(), "side": side}
    spec = load_build_spec(_write_spec(tmp_path / "m.yaml", body))
    assert spec.side == side


# ---------------------------------------------------------------------------
# load_build_spec -- side rejection
# ---------------------------------------------------------------------------


def test_load_missing_side_raises(tmp_path: Path) -> None:
    """A missing ``side`` field is a schema error."""
    body = _minimal_body()
    del body["side"]
    with pytest.raises(SchemaError, match="'side'"):
        load_build_spec(_write_spec(tmp_path / "m.yaml", body))


def test_load_unknown_side_raises(tmp_path: Path) -> None:
    """The ``unknown`` sentinel is rejected -- it would make the mod unmarked."""
    body = {**_minimal_body(), "side": "unknown"}
    with pytest.raises(SchemaError, match="'side' must be one of"):
        load_build_spec(_write_spec(tmp_path / "m.yaml", body))


def test_load_non_string_side_raises(tmp_path: Path) -> None:
    """A non-string side is a schema error."""
    body = {**_minimal_body(), "side": 42}
    with pytest.raises(SchemaError, match="'side' must be one of"):
        load_build_spec(_write_spec(tmp_path / "m.yaml", body))


# ---------------------------------------------------------------------------
# load_build_spec -- dependencies
# ---------------------------------------------------------------------------


def test_load_dependencies_empty_list_is_empty_tuple(tmp_path: Path) -> None:
    """An explicit empty dependencies list yields an empty tuple."""
    body = {**_minimal_body(), "dependencies": []}
    spec = load_build_spec(_write_spec(tmp_path / "m.yaml", body))
    assert spec.dependencies == ()


def test_load_dependencies_multiple(tmp_path: Path) -> None:
    """Multiple dependencies are preserved in declared order."""
    body = {
        **_minimal_body(),
        "dependencies": [
            {"addon_id": "a", "type": "required"},
            {"addon_id": "b", "type": "optional"},
        ],
    }
    spec = load_build_spec(_write_spec(tmp_path / "m.yaml", body))
    assert spec.dependencies == (
        PwTomlDependency(addon_id="a", type="required"),
        PwTomlDependency(addon_id="b", type="optional"),
    )


def test_load_dependencies_non_list_raises(tmp_path: Path) -> None:
    """A non-list dependencies value is a schema error."""
    body = {**_minimal_body(), "dependencies": "not-a-list"}
    with pytest.raises(SchemaError, match="'dependencies' must be a list"):
        load_build_spec(_write_spec(tmp_path / "m.yaml", body))


def test_load_dependencies_missing_addon_id_raises(tmp_path: Path) -> None:
    """A dependency without ``addon_id`` is a schema error."""
    body = {**_minimal_body(), "dependencies": [{"type": "required"}]}
    with pytest.raises(SchemaError, match="addon_id"):
        load_build_spec(_write_spec(tmp_path / "m.yaml", body))


def test_load_dependencies_missing_type_raises(tmp_path: Path) -> None:
    """A dependency without ``type`` is a schema error."""
    body = {**_minimal_body(), "dependencies": [{"addon_id": "a"}]}
    with pytest.raises(SchemaError, match="type"):
        load_build_spec(_write_spec(tmp_path / "m.yaml", body))


def test_load_dependencies_empty_addon_id_raises(tmp_path: Path) -> None:
    """An empty-string ``addon_id`` is a schema error."""
    body = {**_minimal_body(), "dependencies": [{"addon_id": "", "type": "required"}]}
    with pytest.raises(SchemaError, match="addon_id"):
        load_build_spec(_write_spec(tmp_path / "m.yaml", body))


# ---------------------------------------------------------------------------
# load_build_spec -- other failures
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


def test_load_empty_name_raises(tmp_path: Path) -> None:
    """An empty-string ``name`` is a schema error."""
    body = {**_minimal_body(), "name": ""}
    with pytest.raises(SchemaError, match="name"):
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
        "side": "both",
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
    original = _spec(variables={"mod_version": "0.1.0"}, ref="v${mod_version}")
    resolved = resolve_variables(original, {"mod_version": "9.9.9"}, {})
    assert resolved.ref == "v0.1.0"


def test_resolve_override_wins_over_yaml() -> None:
    """``overrides`` wins over YAML and defaults."""
    original = _spec(variables={"tag": "from-yaml"}, ref="${tag}")
    resolved = resolve_variables(original, {"tag": "from-defaults"}, {"tag": "from-override"})
    assert resolved.ref == "from-override"


def test_resolve_undefined_variable_raises() -> None:
    """A ``${undefined}`` reference raises SchemaError."""
    original = _spec(ref="v${missing}")
    with pytest.raises(SchemaError, match="undefined variable \\$\\{missing\\}"):
        resolve_variables(original, {}, {})


def test_resolve_does_not_substitute_side() -> None:
    """``side`` is an enum and is never substituted."""
    original = _spec(side="both")
    resolved = resolve_variables(original, {"side": "client"}, {"side": "server"})
    assert resolved.side == "both"


def test_resolve_does_not_substitute_dependency_type() -> None:
    """Dependency ``type`` is an enum and is never substituted."""
    original = _spec(
        dependencies=(PwTomlDependency(addon_id="dep", type="required"),),
    )
    resolved = resolve_variables(original, {"dep": "x"}, {"type": "optional"})
    assert resolved.dependencies == (PwTomlDependency(addon_id="dep", type="required"),)


def test_resolve_substitutes_dependency_addon_id() -> None:
    """Dependency ``addon_id`` is substituted."""
    original = _spec(
        dependencies=(PwTomlDependency(addon_id="${dep_slug}", type="required"),),
    )
    resolved = resolve_variables(original, {"dep_slug": "flywheel"}, {})
    assert resolved.dependencies == (PwTomlDependency(addon_id="flywheel", type="required"),)


def test_resolve_substitutes_mod_name() -> None:
    """``mod_name`` is substituted."""
    original = _spec(mod_name="Mod ${version}")
    resolved = resolve_variables(original, {"version": "1.0"}, {})
    assert resolved.mod_name == "Mod 1.0"


def test_resolve_brace_syntax_preserved() -> None:
    """Literal ``{...}`` (not ``${...}``) is left untouched."""
    original = _spec(script="build-{date}.sh")
    resolved = resolve_variables(original, {}, {})
    assert resolved.script == "build-{date}.sh"
