# tests/deploy_pack/test_config_model.py

r"""Tests for deploy_pack.config_model, Project_Specs.md §2.9 and §3.1-§3.19.

Coverage, in spec-section order:

  * §2.9  - partition resolution: default-set, unknown, dedup, sort
  * §3.1  - source priority: .env < TOML < CLI
  * §3.2  - compose as a source; stop_grace_period Go-duration parsing
  * §3.4  - TOML wins for www_dir even when compose disagrees
  * §3.5  - compose loading is non-raising; a broken file yields a
            ComposeLoadResult with an error string
  * §3.6  - instance discovery: exact single container_name match
  * §3.9  - docker timing key validation
  * §3.12 - .env is a flat file source; keys verbatim, no folding
  * §3.15 - project-relative path resolution
  * §3.18 - instance root derivation from compose binds
  * §3.19 - www_dir derivation from /usr/share/nginx/ binds
  * §5.11 - in-game template validation at load time
  * §7.1  - output_filename validation
  * §7.5  - download_base_url validation

Only public names from ``config_model`` are exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.deploy_pack.config_model import (
    ComposeLoadResult,
    DockerConfig,
    InstanceConfig,
    derive_instance_root,
    derive_mods_dir,
    derive_www_dir,
    load_compose,
    load_deployment_config,
    match_service_by_container,
    parse_go_duration,
    resolve_compose_path,
    resolve_partition,
    validate_download_base_url,
    validate_in_game_templates,
    validate_output_filename,
)
from minecraft.deploy_pack.errors import ConfigError

# ---------------------------------------------------------------------------
# §3.2: parse_go_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30s", 30),
        ("1m", 60),
        ("1m30s", 90),
        ("2m", 120),
        ("1h", 3600),
        ("1h30m", 5400),
        ("  30s  ", 30),
    ],
)
def test_parse_go_duration_accepts_whole_second_forms(text: str, expected: int) -> None:
    """§3.2: the spec's examples are whole-second tokens; whitespace is stripped."""
    assert parse_go_duration(text) == expected


@pytest.mark.parametrize("text", ["", "30", "abc", "30s xyz", "1.5s", "300ms", "s30", "-30s"])
def test_parse_go_duration_rejects_invalid_forms(text: str) -> None:
    """§3.2: sub-second units and malformed strings are refused, not truncated."""
    with pytest.raises(ValueError):
        parse_go_duration(text)


# ---------------------------------------------------------------------------
# §7.1: validate_output_filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["minecraft_client_{date}.zip", "pack.zip", "a.b.c.zip"])
def test_output_filename_accepts_zip_names(name: str) -> None:
    """§7.1: a single filename ending in .zip is accepted."""
    validate_output_filename(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "pack.tar.gz",
        "pack",
        "pack.zip.bak",
        "dir/pack.zip",
        "dir\\pack.zip",
        "..",
        ".",
        "pack\x00.zip",
    ],
)
def test_output_filename_rejects_unsafe_names(name: str) -> None:
    """§7.1: path separators, traversal components, NUL, and non-.zip suffixes are refused."""
    with pytest.raises(ConfigError):
        validate_output_filename(name)


# ---------------------------------------------------------------------------
# §7.5: validate_download_base_url
# ---------------------------------------------------------------------------


def test_download_base_url_empty_is_error() -> None:
    """§7.5: download_base_url must be non-empty."""
    with pytest.raises(ConfigError):
        validate_download_base_url("")


def test_download_base_url_nonempty_is_accepted() -> None:
    """§7.5: a non-empty base URL passes."""
    validate_download_base_url("http://minecraft/downloads")


# ---------------------------------------------------------------------------
# §5.11: validate_in_game_templates
# ---------------------------------------------------------------------------


def _docker(t_notice: str = "hi {time}", t_cancel: str = "canceled") -> DockerConfig:
    """Return a DockerConfig with the given in-game templates."""
    return DockerConfig(
        compose_file=Path("/dev/null"),
        restart_notice_template=t_notice,
        restart_cancel_notice_template=t_cancel,
    )


def test_in_game_templates_valid_pair() -> None:
    """§5.11: a valid pair passes."""
    validate_in_game_templates(_docker())


def test_in_game_notice_template_empty_is_error() -> None:
    """§5.11: restart_notice_template must be non-empty."""
    with pytest.raises(ConfigError):
        validate_in_game_templates(_docker(t_notice=""))


def test_in_game_notice_template_unknown_placeholder_is_error() -> None:
    """§5.11: only {time} is permitted in restart_notice_template."""
    with pytest.raises(ConfigError):
        validate_in_game_templates(_docker(t_notice="hi {name}"))


def test_in_game_notice_template_multiple_time_ok() -> None:
    """§5.11: {time} may appear more than once."""
    validate_in_game_templates(_docker(t_notice="{time} and again {time}"))


def test_in_game_cancel_template_empty_is_error() -> None:
    """§5.11: restart_cancel_notice_template must be non-empty."""
    with pytest.raises(ConfigError):
        validate_in_game_templates(_docker(t_cancel=""))


def test_in_game_cancel_template_with_placeholder_is_error() -> None:
    """§5.11: restart_cancel_notice_template must not contain any placeholders."""
    with pytest.raises(ConfigError):
        validate_in_game_templates(_docker(t_cancel="oops {time}"))


# ---------------------------------------------------------------------------
# §3.5: load_compose is non-raising
# ---------------------------------------------------------------------------


def _write_compose(tmp_path: Path, body: str) -> Path:
    """Write a compose file to a temp path and return it."""
    p = tmp_path / "docker-compose.yml"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_compose_missing_file_is_non_raising(tmp_path: Path) -> None:
    """§3.5: a missing compose file yields an error string, not a raise."""
    result = load_compose(tmp_path / "nope.yml")
    assert isinstance(result, ComposeLoadResult)
    assert result.file is None
    assert result.error is not None
    assert not result.ok


def test_load_compose_malformed_is_non_raising(tmp_path: Path) -> None:
    """§3.5: malformed YAML yields an error string, not a raise."""
    p = _write_compose(tmp_path, ":::not yaml")
    result = load_compose(p)
    assert result.file is None
    assert result.error is not None


def test_load_compose_empty_is_non_raising(tmp_path: Path) -> None:
    """§3.5: an empty compose file yields an error string, not a raise."""
    p = _write_compose(tmp_path, "")
    result = load_compose(p)
    assert result.file is None
    assert result.error is not None


# ---------------------------------------------------------------------------
# §3.2 / §3.18: compose parsing
# ---------------------------------------------------------------------------


def test_compose_short_form_binds(tmp_path: Path) -> None:
    """§3.2: short-form volume entries with host paths are parsed as binds."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n"
        "  mc:\n"
        "    container_name: mc-survival\n"
        "    volumes:\n"
        "      - ./data:/data\n"
        "      - ./shared/mods:/data/mods\n"
        "      - named_vol:/something\n"
        "volumes:\n"
        "  named_vol: {}\n",
    )
    result = load_compose(p)
    assert result.ok
    svc = result.file.services["mc"]
    targets = {b.container_target for b in svc.binds}
    assert targets == {"/data", "/data/mods"}


def test_compose_long_form_bind_excludes_volumes(tmp_path: Path) -> None:
    """§3.2: long-form bind entries are kept; volume entries are excluded."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n"
        "  mc:\n"
        "    container_name: mc\n"
        "    volumes:\n"
        "      - type: bind\n"
        "        source: ./data\n"
        "        target: /data\n"
        "      - type: volume\n"
        "        source: named\n"
        "        target: /other\n",
    )
    result = load_compose(p)
    assert result.ok
    svc = result.file.services["mc"]
    assert len(svc.binds) == 1
    assert svc.binds[0].container_target == "/data"


def test_compose_healthcheck_presence_is_recorded(tmp_path: Path) -> None:
    """§3.8: the has_healthcheck flag reflects the presence of a compose healthcheck."""
    p = _write_compose(
        tmp_path,
        '\nservices:\n  with_hc:\n    container_name: a\n    healthcheck:\n      test: ["CMD", "true"]\n  without_hc:\n    container_name: b\n',
    )
    result = load_compose(p)
    assert result.file.services["with_hc"].has_healthcheck is True
    assert result.file.services["without_hc"].has_healthcheck is False


def test_compose_environment_dict_and_list_forms(tmp_path: Path) -> None:
    """§3.2: environment accepts dict and list forms; bare list items are ignored."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n"
        "  a:\n"
        "    container_name: a\n"
        "    environment:\n"
        '      RCON_PORT: "25575"\n'
        "      OTHER: x\n"
        "  b:\n"
        "    container_name: b\n"
        "    environment:\n"
        "      - RCON_PORT=25576\n"
        "      - BARE\n",
    )
    result = load_compose(p)
    assert result.file.services["a"].environment["RCON_PORT"] == "25575"
    assert result.file.services["b"].environment["RCON_PORT"] == "25576"
    assert "BARE" not in result.file.services["b"].environment


def test_compose_secrets_short_and_long_forms(tmp_path: Path) -> None:
    """§3.2: both secret declaration forms yield the secret's source name."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n"
        "  a:\n"
        "    container_name: a\n"
        "    secrets:\n"
        "      - rcon_password\n"
        "  b:\n"
        "    container_name: b\n"
        "    secrets:\n"
        "      - source: rcon_password\n"
        "        target: /run/secrets/rcon\n"
        "secrets:\n"
        "  rcon_password:\n"
        "    file: ./secrets/rcon.txt\n",
    )
    result = load_compose(p)
    assert result.file.services["a"].secrets == ["rcon_password"]
    assert result.file.services["b"].secrets == ["rcon_password"]
    assert result.file.secret_files["rcon_password"] == tmp_path / "secrets" / "rcon.txt"


def test_compose_env_file_list_resolved_against_base_dir(tmp_path: Path) -> None:
    """§3.2: env_file entries are resolved relative to the compose file's directory."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  a:\n    container_name: a\n    env_file:\n      - ./a.env\n      - /abs/b.env\n",
    )
    result = load_compose(p)
    files = result.file.services["a"].env_files
    assert files == [tmp_path / "a.env", Path("/abs/b.env")]


# ---------------------------------------------------------------------------
# §3.6: match_service_by_container
# ---------------------------------------------------------------------------


def test_match_service_unique(tmp_path: Path) -> None:
    """§3.6: exactly one service with the given container_name is returned."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  a:\n    container_name: mc-a\n  b:\n    container_name: mc-b\n",
    )
    compose = load_compose(p).file
    assert match_service_by_container(compose, "mc-b").name == "b"


def test_match_service_zero_matches_is_error(tmp_path: Path) -> None:
    """§3.6: zero matches is a configuration error."""
    p = _write_compose(tmp_path, "\nservices:\n  a:\n    container_name: mc-a\n")
    compose = load_compose(p).file
    with pytest.raises(ConfigError):
        match_service_by_container(compose, "mc-nope")


def test_match_service_multiple_matches_is_error(tmp_path: Path) -> None:
    """§3.6: multiple matches is a configuration error."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  a:\n    container_name: mc\n  b:\n    container_name: mc\n",
    )
    compose = load_compose(p).file
    with pytest.raises(ConfigError):
        match_service_by_container(compose, "mc")


# ---------------------------------------------------------------------------
# §3.19: derive_www_dir
# ---------------------------------------------------------------------------


def test_derive_www_dir_single_match(tmp_path: Path) -> None:
    """§3.19: a unique bind source with a /usr/share/nginx/ target is www_dir."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  nginx:\n    container_name: nginx\n    volumes:\n      - ./www:/usr/share/nginx/html\n",
    )
    result = derive_www_dir(load_compose(p).file)
    assert result.path == Path("./www")
    assert result.error is None


def test_derive_www_dir_exact_target_without_subpath_is_ignored(tmp_path: Path) -> None:
    """§3.19: /usr/share/nginx with no subpath is not a candidate."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  nginx:\n    container_name: nginx\n    volumes:\n      - ./www:/usr/share/nginx\n      - ./other:/etc/nginx\n",
    )
    result = derive_www_dir(load_compose(p).file)
    assert result.path is None
    assert result.error is not None


def test_derive_www_dir_zero_candidates_is_non_raising(tmp_path: Path) -> None:
    """§3.19: zero candidates yields a diagnostic, not a raise."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  nginx:\n    container_name: nginx\n    volumes:\n      - ./www:/usr/share/nginx\n",
    )
    result = derive_www_dir(load_compose(p).file)
    assert result.path is None
    assert result.error is not None


def test_derive_www_dir_multiple_candidates_is_non_raising(tmp_path: Path) -> None:
    """§3.19: multiple candidates yields the list and a diagnostic."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  nginx:\n    container_name: nginx\n    volumes:\n      - ./www1:/usr/share/nginx/a\n      - ./www2:/usr/share/nginx/b\n",
    )
    result = derive_www_dir(load_compose(p).file)
    assert result.path is None
    assert result.error is not None
    assert len(result.candidates) == 2


# ---------------------------------------------------------------------------
# §3.18: derive_instance_root
# ---------------------------------------------------------------------------


def _instance(name: str) -> InstanceConfig:
    """Return a bare InstanceConfig for partition tests."""
    return InstanceConfig(name=name, container=f"mc-{name}")


def test_derive_instance_root_prefers_exact_slash_data_bind(tmp_path: Path) -> None:
    """§3.18: a bind whose target is exactly /data gives the instance root directly."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  mc:\n    container_name: mc\n    volumes:\n      - ./data/survival:/data\n      - ./shared:/data/mods\n",
    )
    svc = load_compose(p).file.services["mc"]
    assert derive_instance_root(svc) == Path("./data/survival")


def test_derive_instance_root_infers_from_subpath_binds(tmp_path: Path) -> None:
    """§3.18: subpath binds imply a common host prefix."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n"
        "  mc:\n"
        "    container_name: mc\n"
        "    volumes:\n"
        "      - ./data/survival/config:/data/config\n"
        "      - ./data/survival/kubejs:/data/kubejs\n"
        "      - ./data/survival/server.properties:/data/server.properties\n"
        "      - ./shared:/data/mods\n",
    )
    svc = load_compose(p).file.services["mc"]
    assert derive_instance_root(svc) == Path("./data/survival")


def test_derive_instance_root_returns_none_when_ambiguous(tmp_path: Path) -> None:
    """§3.18: equally-supported candidate roots are treated as ambiguous."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  mc:\n    container_name: mc\n    volumes:\n      - ./a/config:/data/config\n      - ./b/kubejs:/data/kubejs\n",
    )
    svc = load_compose(p).file.services["mc"]
    assert derive_instance_root(svc) is None


def test_derive_mods_dir_from_data_mods_bind(tmp_path: Path) -> None:
    """§3.18: mods_dir is derived independently from a /data/mods bind."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  mc:\n    container_name: mc\n    volumes:\n      - ./data/survival:/data\n      - ./shared/mods:/data/mods\n",
    )
    svc = load_compose(p).file.services["mc"]
    assert derive_mods_dir(svc) == Path("./shared/mods")


# ---------------------------------------------------------------------------
# §2.9: resolve_partition
# ---------------------------------------------------------------------------


def test_resolve_partition_none_returns_all_sorted() -> None:
    """§2.9: with no --instance, the partition is every configured instance, sorted."""
    instances = {"survival": _instance("survival"), "creative": _instance("creative"), "amplified": _instance("amplified")}
    partition, unknown = resolve_partition(instances, None)
    assert partition == ["amplified", "creative", "survival"]
    assert unknown == []


def test_resolve_partition_explicit_subset_sorted() -> None:
    """§2.9: requested names are deduplicated and sorted lexicographically."""
    instances = {"survival": _instance("survival"), "creative": _instance("creative"), "amplified": _instance("amplified")}
    partition, unknown = resolve_partition(instances, {"survival", "creative"})
    assert partition == ["creative", "survival"]
    assert unknown == []


def test_resolve_partition_unknown_names_are_returned_not_raised() -> None:
    """§2.9: preflight raises for unknown names; resolve_partition does not."""
    partition, unknown = resolve_partition({"survival": _instance("survival")}, {"survival", "nope"})
    assert partition == ["survival"]
    assert unknown == ["nope"]


def test_resolve_partition_all_unknown() -> None:
    """§2.9: a request set of only unknown names yields an empty partition."""
    partition, unknown = resolve_partition({"survival": _instance("survival")}, {"a", "b"})
    assert partition == []
    assert unknown == ["a", "b"]


def test_resolve_partition_deduplicates_repeated_names() -> None:
    """§2.9: duplicates collapse."""
    instances = {"survival": _instance("survival"), "creative": _instance("creative")}
    partition, unknown = resolve_partition(instances, ["survival", "survival", "creative", "creative"])
    assert partition == ["creative", "survival"]
    assert unknown == []


def test_resolve_partition_empty_set_is_empty() -> None:
    """§2.9: an empty request set means an empty partition."""
    partition, unknown = resolve_partition({"survival": _instance("survival")}, set())
    assert partition == []
    assert unknown == []


# ---------------------------------------------------------------------------
# §3.2: resolve_compose_path
# ---------------------------------------------------------------------------


def test_resolve_compose_path_relative_against_base_dir(tmp_path: Path) -> None:
    """§3.2: relative compose source paths resolve against the compose file's directory."""
    base = tmp_path / "compose_dir"
    base.mkdir()
    assert resolve_compose_path(Path("./data"), base) == (base / "data").resolve()


def test_resolve_compose_path_absolute_is_resolved(tmp_path: Path) -> None:
    """§3.2: absolute compose source paths are returned resolved."""
    abs_path = (tmp_path / "abs").resolve()
    abs_path.mkdir()
    assert resolve_compose_path(abs_path, tmp_path) == abs_path


# ---------------------------------------------------------------------------
# load_deployment_config: end-to-end
# ---------------------------------------------------------------------------


def _write_full_repo(tmp_path: Path) -> Path:
    """Create a minimal valid project tree. Return the config.d path."""
    project_root = tmp_path
    config_dir = project_root / "config.d"
    config_dir.mkdir()
    (project_root / "docker-compose.yml").write_text(
        "\n"
        "services:\n"
        "  mc-survival:\n"
        "    container_name: mc-survival\n"
        '    healthcheck:\n      test: ["CMD", "true"]\n'
        "    volumes:\n"
        "      - ./data/survival:/data\n"
        "      - ./shared/mods:/data/mods\n"
        "    stop_grace_period: 30s\n"
        "  nginx:\n"
        "    container_name: nginx\n"
        "    volumes:\n"
        "      - ./www:/usr/share/nginx/html\n"
        "secrets:\n"
        "  rcon_password:\n"
        "    file: ./secrets/rcon.txt\n",
        encoding="utf-8",
    )
    (config_dir / "deploy_pack.toml").write_text(
        "\n"
        'instance_discovery = "explicit"\n'
        'sync_root    = "./sync"\n'
        'modpack_dir  = "./sync/downloads"\n'
        'output_filename   = "minecraft_client_{date}.zip"\n'
        'download_base_url = "http://minecraft/downloads"\n'
        'protect_file = "./.deploy_protect"\n'
        "\n[sync_mapping]\n"
        'config = "config"\n'
        'kubejs = "kubejs"\n'
        "\n[docker]\n"
        'compose_file = "./docker-compose.yml"\n'
        "\n[instances.survival]\n"
        'container = "mc-survival"\n'
        'config_mode = "merge"\n'
        'kubejs_mode = "delete"\n'
        "\n[resource_pack.survival]\n"
        'filename = "pack.zip"\n'
        "required = true\n"
        'prompt   = ""\n',
        encoding="utf-8",
    )
    return config_dir


def test_load_minimal_repo_populates_expected_fields(tmp_path: Path) -> None:
    """§3.1, §3.15, §3.18, §3.19: paths resolve against the project root; compose derives www_dir and instance roots."""
    cfg = load_deployment_config(_write_full_repo(tmp_path))
    assert cfg.project_root == tmp_path
    assert cfg.sync_root == (tmp_path / "sync").resolve()
    assert cfg.modpack_dir == (tmp_path / "sync" / "downloads").resolve()
    assert cfg.www_dir == (tmp_path / "www").resolve()
    assert cfg.www_dir_error is None
    assert cfg.output_filename == "minecraft_client_{date}.zip"
    assert cfg.download_base_url == "http://minecraft/downloads"
    assert cfg.protect_file == (tmp_path / ".deploy_protect").resolve()
    assert cfg.compose.ok
    inst = cfg.instances["survival"]
    assert inst.container == "mc-survival"
    assert inst.instance_root == (tmp_path / "data" / "survival").resolve()
    assert inst.config_path == tmp_path / "data" / "survival" / "config"
    assert inst.kubejs_path == tmp_path / "data" / "survival" / "kubejs"
    assert inst.server_properties_path == tmp_path / "data" / "survival" / "server.properties"
    assert inst.stop_grace_seconds == 30
    assert inst.stop_grace_parse_error is None
    assert inst.service_match_error is None
    assert cfg.resource_packs["survival"].filename == "pack.zip"
    assert cfg.partition == ["survival"]
    assert cfg.partition_unknown == []
    assert cfg.requested_instances is None
    assert cfg.partition_requested is False


def test_load_partition_with_unknown_names(tmp_path: Path) -> None:
    """§2.9: unknown requested names are surfaced for preflight to evaluate."""
    cfg = load_deployment_config(_write_full_repo(tmp_path), requested_instances={"survival", "nope"})
    assert cfg.partition == ["survival"]
    assert cfg.partition_unknown == ["nope"]
    assert cfg.requested_instances == {"survival", "nope"}
    assert cfg.partition_requested is True


def test_load_records_instance_match_error(tmp_path: Path) -> None:
    """§3.6: no matching service is recorded on the instance, not raised."""
    config_dir = _write_full_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace('container = "mc-survival"', 'container = "mc-nope"'), encoding="utf-8")
    cfg = load_deployment_config(config_dir)
    inst = cfg.instances["survival"]
    assert inst.service is None
    assert inst.service_match_error is not None
    assert inst.instance_root is None


def test_load_records_stop_grace_parse_error(tmp_path: Path) -> None:
    """§3.2: an unparseable stop_grace_period is recorded, not raised."""
    config_dir = _write_full_repo(tmp_path)
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(compose.read_text(encoding="utf-8").replace("stop_grace_period: 30s", "stop_grace_period: 1.5s"), encoding="utf-8")
    cfg = load_deployment_config(config_dir)
    inst = cfg.instances["survival"]
    assert inst.stop_grace_period_raw == "1.5s"
    assert inst.stop_grace_parse_error is not None
    assert inst.stop_grace_seconds == 10


def test_load_broken_compose_does_not_raise(tmp_path: Path) -> None:
    """§3.5: a broken compose is carried on the config for preflight to evaluate."""
    config_dir = _write_full_repo(tmp_path)
    (tmp_path / "docker-compose.yml").write_text(":::not yaml", encoding="utf-8")
    cfg = load_deployment_config(config_dir)
    assert not cfg.compose.ok
    assert cfg.compose.error is not None
    assert cfg.www_dir is None
    assert cfg.www_dir_error is not None


def test_load_toml_www_dir_wins_over_broken_compose(tmp_path: Path) -> None:
    """§3.4: TOML www_dir wins regardless of compose state."""
    config_dir = _write_full_repo(tmp_path)
    (tmp_path / "docker-compose.yml").write_text(":::not yaml", encoding="utf-8")
    toml = config_dir / "deploy_pack.toml"
    toml.write_text('www_dir = "./override_www"\n' + toml.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_deployment_config(config_dir)
    assert cfg.www_dir == (tmp_path / "override_www").resolve()
    assert cfg.www_dir_error is None


def test_load_www_dir_undeterminable_is_recorded(tmp_path: Path) -> None:
    """§3.19: compose present but no /usr/share/nginx/ bind means www_dir is undeterminable."""
    config_dir = _write_full_repo(tmp_path)
    (tmp_path / "docker-compose.yml").write_text(
        "\nservices:\n"
        "  mc-survival:\n"
        "    container_name: mc-survival\n"
        '    healthcheck:\n      test: ["CMD", "true"]\n'
        "    volumes:\n"
        "      - ./data/survival:/data\n"
        "      - ./shared/mods:/data/mods\n",
        encoding="utf-8",
    )
    cfg = load_deployment_config(config_dir)
    assert cfg.compose.ok
    assert cfg.www_dir is None
    assert cfg.www_dir_error is not None


def test_load_env_flat_keys_used_verbatim(tmp_path: Path) -> None:
    """§3.12: .env keys are used verbatim; no prefix stripping or case folding."""
    config_dir = _write_full_repo(tmp_path)
    (config_dir / ".env").write_text("webhook_url=https://discord.example/webhook\n", encoding="utf-8")
    cfg = load_deployment_config(config_dir)
    assert cfg.webhook_url == "https://discord.example/webhook"


def test_load_env_does_not_override_toml(tmp_path: Path) -> None:
    """§3.1: TOML has higher priority than .env for the same key."""
    config_dir = _write_full_repo(tmp_path)
    (config_dir / ".env").write_text("output_filename=from_env.zip\n", encoding="utf-8")
    assert load_deployment_config(config_dir).output_filename == "minecraft_client_{date}.zip"


def test_load_missing_env_file_is_silent(tmp_path: Path) -> None:
    """§3.12: an absent .env is not an error."""
    config_dir = _write_full_repo(tmp_path)
    assert not (config_dir / ".env").exists()
    assert load_deployment_config(config_dir).webhook_url is None


def test_load_env_keys_are_case_sensitive(tmp_path: Path) -> None:
    """§3.12: keys are not folded to lowercase."""
    config_dir = _write_full_repo(tmp_path)
    (config_dir / ".env").write_text("WEBHOOK_URL=https://discord.example/webhook\n", encoding="utf-8")
    assert load_deployment_config(config_dir).webhook_url is None


def test_load_env_malformed_lines_are_skipped(tmp_path: Path) -> None:
    """§3.12: malformed lines (no '=') are silently skipped by the parser."""
    config_dir = _write_full_repo(tmp_path)
    (config_dir / ".env").write_text(
        "# a comment\n\nwebhook_url=https://discord.example/webhook\nthis line has no equals sign\nanother_url=https://example\n",
        encoding="utf-8",
    )
    cfg = load_deployment_config(config_dir)
    assert cfg.webhook_url == "https://discord.example/webhook"


def test_load_cli_overlay_overrides_toml(tmp_path: Path) -> None:
    """§3.1: CLI arguments outrank the TOML file."""
    config_dir = _write_full_repo(tmp_path)
    cfg = load_deployment_config(config_dir, cli_remaining=["--output-filename", "custom_{date}.zip"])
    assert cfg.output_filename == "custom_{date}.zip"


def test_load_cli_overlay_equals_form(tmp_path: Path) -> None:
    """§3.1: --key=value is accepted by the CLI overlay parser."""
    config_dir = _write_full_repo(tmp_path)
    cfg = load_deployment_config(config_dir, cli_remaining=["--download-base-url=http://other/"])
    assert cfg.download_base_url == "http://other/"


def test_load_empty_download_base_url_raises(tmp_path: Path) -> None:
    """§7.5: an empty download_base_url is exit 3."""
    config_dir = _write_full_repo(tmp_path)
    with pytest.raises(ConfigError):
        load_deployment_config(config_dir, cli_remaining=["--download-base-url="])


def test_load_orphan_resource_pack_section_raises(tmp_path: Path) -> None:
    """§7.9: an orphan [resource_pack.X] with no matching [instances.X] is exit 3."""
    config_dir = _write_full_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + '\n\n[resource_pack.orphan]\nfilename = "x.zip"\nrequired = false\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_deployment_config(config_dir)


def test_load_bad_output_filename_raises(tmp_path: Path) -> None:
    """§7.1: output_filename must end in .zip."""
    config_dir = _write_full_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            'output_filename   = "minecraft_client_{date}.zip"',
            'output_filename   = "pack.tar.gz"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_deployment_config(config_dir)


def test_load_negative_restart_wait_seconds_raises(tmp_path: Path) -> None:
    """§3.9: restart_wait_seconds must be >= 0."""
    config_dir = _write_full_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            '[docker]\ncompose_file = "./docker-compose.yml"',
            '[docker]\ncompose_file = "./docker-compose.yml"\nrestart_wait_seconds = -1',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_deployment_config(config_dir)


def test_load_zero_health_poll_seconds_raises(tmp_path: Path) -> None:
    """§3.9: health_poll_seconds must be > 0."""
    config_dir = _write_full_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            '[docker]\ncompose_file = "./docker-compose.yml"',
            '[docker]\ncompose_file = "./docker-compose.yml"\nhealth_poll_seconds = 0',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_deployment_config(config_dir)


def test_load_missing_required_resource_pack_key_raises(tmp_path: Path) -> None:
    """§3.9: [resource_pack.X] requires filename and required."""
    config_dir = _write_full_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace("required = true", ""), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_deployment_config(config_dir)
