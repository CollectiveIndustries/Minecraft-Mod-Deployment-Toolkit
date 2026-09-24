# tests/deploy_pack/test_config_model.py

"""Tests for deploy_pack.config_model, per Project_Specs.md v3.0 §10.1.

Coverage areas:
  * parse_go_duration: valid / invalid Go durations
  * output_filename / download_base_url validation
  * in-game template validation (empty / unknown placeholder /
    cancel-with-placeholder)
  * compose parsing: binds (short + long), secrets (short + long),
    healthcheck presence, env (dict + list), env_file (str + list)
  * compose loading is non-raising: missing file and malformed YAML
    return a ComposeLoadResult with an error string (§3.5, §4.3)
  * www_dir derivation is non-raising: zero, multiple, and the
    exact-/usr/share/nginx-ignored case (§3.19)
  * instance derivation: /data bind, stop_grace_period parse; failures
    are recorded, not raised (§3.6, §3.2)
  * resolve_partition: default-set, unknown, dedup, sort (§2.9)
  * .env as a flat file source: loads, TOML overrides, missing is
    silent, malformed lines are skipped (§3.1, §3.12)
  * CLI overlay precedence (§3.1)
  * resolve_compose_path: public alias for compose-base-dir resolution
  * partition_requested / requested_instances: --instance presence flag
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.deploy_pack.config_model import (
    ComposeLoadResult,
    DockerConfig,
    InstanceConfig,
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


@pytest.mark.parametrize("text,expected", [("30s", 30), ("1m", 60), ("1m30s", 90), ("2m", 120), ("1h", 3600), ("1h30m", 5400), ("  30s  ", 30)])
def test_parse_go_duration_valid(text: str, expected: int) -> None:
    """Tests that parse_go_duration parses valid inputs correctly."""
    assert parse_go_duration(text) == expected


@pytest.mark.parametrize("text", ["", "30", "abc", "30s xyz", "1.5s", "300ms", "s30", "-30s"])
def test_parse_go_duration_invalid(text: str) -> None:
    """Tests that parsing an invalid Go duration raises ValueError."""
    with pytest.raises(ValueError):
        parse_go_duration(text)


@pytest.mark.parametrize("name", ["minecraft_client_{date}.zip", "pack.zip", "a.b.c.zip"])
def test_output_filename_valid(name: str) -> None:
    """Tests that a valid output filename passes validation."""
    validate_output_filename(name)


@pytest.mark.parametrize("name", ["", "pack.tar.gz", "pack", "pack.zip.bak", "dir/pack.zip", "dir\\pack.zip", "..", ".", "pack\x00.zip"])
def test_output_filename_invalid(name: str) -> None:
    """Tests that an invalid output filename raises a ConfigError during validation."""
    with pytest.raises(ConfigError):
        validate_output_filename(name)


def test_download_base_url_empty_is_error() -> None:
    """Tests that validate_download_base_url raises ConfigError for an empty string."""
    with pytest.raises(ConfigError):
        validate_download_base_url("")


def test_download_base_url_nonempty_ok() -> None:
    """Tests that validate_download_base_url accepts a non-empty URL string."""
    validate_download_base_url("http://minecraft/downloads")


def _docker(t_notice: str = "hi {time}", t_cancel: str = "canceled") -> DockerConfig:
    return DockerConfig(compose_file=Path("/dev/null"), restart_notice_template=t_notice, restart_cancel_notice_template=t_cancel)


def test_in_game_templates_valid() -> None:
    """Test that valid in-game templates pass validation without raising an error."""
    validate_in_game_templates(_docker())


def test_in_game_notice_template_empty_is_error() -> None:
    """Test that an empty notice template raises ConfigError."""
    with pytest.raises(ConfigError):
        validate_in_game_templates(_docker(t_notice=""))


def test_in_game_notice_template_unknown_placeholder_is_error() -> None:
    """Test that a notice template with an unknown placeholder raises ConfigError."""
    with pytest.raises(ConfigError):
        validate_in_game_templates(_docker(t_notice="hi {name}"))


def test_in_game_notice_template_multiple_time_ok() -> None:
    """Test that a notice template may contain the {time} placeholder multiple times."""
    validate_in_game_templates(_docker(t_notice="{time} and again {time}"))


def test_in_game_cancel_template_placeholder_is_error() -> None:
    """Test that a cancel template with a placeholder not allowed in cancel templates raises ConfigError."""
    with pytest.raises(ConfigError):
        validate_in_game_templates(_docker(t_cancel="oops {time}"))


def test_in_game_cancel_template_empty_is_error() -> None:
    """Tests that an empty in-game cancel template raises ConfigError."""
    with pytest.raises(ConfigError):
        validate_in_game_templates(_docker(t_cancel=""))


def _write_compose(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "docker-compose.yml"
    p.write_text(body, encoding="utf-8")
    return p


def test_compose_short_form_binds(tmp_path: Path) -> None:
    """Tests that short-form Compose volume entries are parsed into bind targets."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  mc:\n    container_name: mc-survival\n    volumes:\n      - ./data:/data\n      - ./shared/mods:/data/mods\n      - named_vol:/something\nvolumes:\n  named_vol: {}\n",
    )
    result = load_compose(p)
    assert result.ok
    svc = result.file.services["mc"]
    targets = {b.container_target for b in svc.binds}
    assert targets == {"/data", "/data/mods"}


def test_compose_long_form_bind(tmp_path: Path) -> None:
    """Tests that long-form Compose bind entries are parsed while volume entries are excluded."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  mc:\n    container_name: mc\n    volumes:\n      - type: bind\n        source: ./data\n        target: /data\n      - type: volume\n        source: named\n        target: /other\n",
    )
    result = load_compose(p)
    assert result.ok
    svc = result.file.services["mc"]
    assert len(svc.binds) == 1
    assert svc.binds[0].container_target == "/data"


def test_compose_healthcheck_presence(tmp_path: Path) -> None:
    """Tests that has_healthcheck reflects the presence of a Compose healthcheck."""
    p = _write_compose(
        tmp_path, '\nservices:\n  with_hc:\n    container_name: a\n    healthcheck:\n      test: ["CMD", "true"]\n  without_hc:\n    container_name: b\n'
    )
    result = load_compose(p)
    assert result.file.services["with_hc"].has_healthcheck is True
    assert result.file.services["without_hc"].has_healthcheck is False


def test_compose_environment_dict_and_list(tmp_path: Path) -> None:
    """Tests that Compose environments support dict and list forms, ignoring bare keys."""
    p = _write_compose(
        tmp_path,
        '\nservices:\n  a:\n    container_name: a\n    environment:\n      RCON_PORT: "25575"\n      OTHER: x\n  b:\n    container_name: b\n    environment:\n      - RCON_PORT=25576\n      - BARE\n',
    )
    result = load_compose(p)
    assert result.file.services["a"].environment["RCON_PORT"] == "25575"
    assert result.file.services["b"].environment["RCON_PORT"] == "25576"
    assert "BARE" not in result.file.services["b"].environment


def test_compose_secrets_short_and_long(tmp_path: Path) -> None:
    """Tests that short and long secret syntax both resolve to the secret name and its file path."""
    p = _write_compose(
        tmp_path,
        "\nservices:\n  a:\n    container_name: a\n    secrets:\n      - rcon_password\n  b:\n    container_name: b\n    secrets:\n      - source: rcon_password\n        target: /run/secrets/rcon\nsecrets:\n  rcon_password:\n    file: ./secrets/rcon.txt\n",
    )
    result = load_compose(p)
    assert result.file.services["a"].secrets == ["rcon_password"]
    assert result.file.services["b"].secrets == ["rcon_password"]
    assert result.file.secret_files["rcon_password"] == tmp_path / "secrets" / "rcon.txt"


def test_compose_env_file_list(tmp_path: Path) -> None:
    """Tests that a list of env_file entries is resolved to relative and absolute paths."""
    p = _write_compose(tmp_path, "\nservices:\n  a:\n    container_name: a\n    env_file:\n      - ./a.env\n      - /abs/b.env\n")
    result = load_compose(p)
    files = result.file.services["a"].env_files
    assert files == [tmp_path / "a.env", Path("/abs/b.env")]


def test_compose_missing_file_is_non_raising(tmp_path: Path) -> None:
    """§3.5, §4.3: broken compose must not short-circuit at config load."""
    result = load_compose(tmp_path / "nope.yml")
    assert isinstance(result, ComposeLoadResult)
    assert result.file is None
    assert result.error is not None
    assert not result.ok


def test_compose_malformed_is_non_raising(tmp_path: Path) -> None:
    """Tests that malformed compose content returns a result with an error instead of raising."""
    p = _write_compose(tmp_path, ":::not yaml")
    result = load_compose(p)
    assert result.file is None
    assert result.error is not None
    assert not result.ok


def test_compose_empty_is_non_raising(tmp_path: Path) -> None:
    """Tests that empty compose content returns a result with an error instead of raising."""
    p = _write_compose(tmp_path, "")
    result = load_compose(p)
    assert result.file is None
    assert result.error is not None


def test_match_service_unique(tmp_path: Path) -> None:
    """Tests that a service is matched by its unique container name."""
    p = _write_compose(tmp_path, "\nservices:\n  a:\n    container_name: mc-a\n  b:\n    container_name: mc-b\n")
    compose = load_compose(p).file
    svc = match_service_by_container(compose, "mc-b")
    assert svc.name == "b"


def test_match_service_zero(tmp_path: Path) -> None:
    """Tests that matching by container name raises ConfigError when no service matches."""
    p = _write_compose(tmp_path, "\nservices:\n  a:\n    container_name: mc-a\n")
    compose = load_compose(p).file
    with pytest.raises(ConfigError):
        match_service_by_container(compose, "mc-nope")


def test_match_service_multiple(tmp_path: Path) -> None:
    """Tests that matching by container name raises ConfigError when multiple services match."""
    p = _write_compose(tmp_path, "\nservices:\n  a:\n    container_name: mc\n  b:\n    container_name: mc\n")
    compose = load_compose(p).file
    with pytest.raises(ConfigError):
        match_service_by_container(compose, "mc")


def test_derive_www_dir_single(tmp_path: Path) -> None:
    """Tests that derive_www_dir returns the correct path for a single matching volume."""
    p = _write_compose(tmp_path, "\nservices:\n  nginx:\n    container_name: nginx\n    volumes:\n      - ./www:/usr/share/nginx/html\n")
    compose = load_compose(p).file
    result = derive_www_dir(compose)
    assert result.path == Path("./www")
    assert result.error is None


def test_derive_www_dir_zero_is_non_raising(tmp_path: Path) -> None:
    """Tests that derive_www_dir returns an error without raising when no volume matches."""
    p = _write_compose(tmp_path, "\nservices:\n  nginx:\n    container_name: nginx\n    volumes:\n      - ./www:/usr/share/nginx\n")
    compose = load_compose(p).file
    result = derive_www_dir(compose)
    assert result.path is None
    assert result.error is not None


def test_derive_www_dir_multiple_is_non_raising(tmp_path: Path) -> None:
    """Tests that derive_www_dir returns an error with candidates without raising when multiple volumes match."""
    p = _write_compose(
        tmp_path, "\nservices:\n  nginx:\n    container_name: nginx\n    volumes:\n      - ./www1:/usr/share/nginx/a\n      - ./www2:/usr/share/nginx/b\n"
    )
    compose = load_compose(p).file
    result = derive_www_dir(compose)
    assert result.path is None
    assert result.error is not None
    assert len(result.candidates) == 2


def test_derive_www_dir_exact_no_subpath_ignored(tmp_path: Path) -> None:
    """Tests that a volume mount without a www subpath is ignored, yielding no path and an error."""
    p = _write_compose(tmp_path, "\nservices:\n  nginx:\n    container_name: nginx\n    volumes:\n      - ./www:/usr/share/nginx\n      - ./other:/etc/nginx\n")
    compose = load_compose(p).file
    result = derive_www_dir(compose)
    assert result.path is None
    assert result.error is not None


def _inst(name: str) -> InstanceConfig:
    return InstanceConfig(name=name, container=f"mc-{name}")


def test_resolve_partition_none_returns_all_sorted() -> None:
    """Tests that passing None returns all available partitions sorted alphabetically with no unknowns."""
    instances = {"survival": _inst("survival"), "creative": _inst("creative"), "amplified": _inst("amplified")}
    partition, unknown = resolve_partition(instances, None)
    assert partition == ["amplified", "creative", "survival"]
    assert unknown == []


def test_resolve_partition_explicit_subset() -> None:
    """Tests that an explicit subset of requested names resolves to the matching partitions and no unknowns."""
    instances = {"survival": _inst("survival"), "creative": _inst("creative"), "amplified": _inst("amplified")}
    partition, unknown = resolve_partition(instances, {"survival", "creative"})
    assert partition == ["creative", "survival"]
    assert unknown == []


def test_resolve_partition_unknown_names_returned_not_raised() -> None:
    """Tests that unknown partition names are returned in the unknown list rather than raising an exception."""
    instances = {"survival": _inst("survival")}
    partition, unknown = resolve_partition(instances, {"survival", "nope"})
    assert partition == ["survival"]
    assert unknown == ["nope"]


def test_resolve_partition_all_unknown() -> None:
    """Tests that requesting only unknown partition names returns an empty partition and all names as unknown."""
    instances = {"survival": _inst("survival")}
    partition, unknown = resolve_partition(instances, {"a", "b"})
    assert partition == []
    assert unknown == ["a", "b"]


def test_resolve_partition_deduplicates() -> None:
    """Tests that resolve_partition removes duplicate instance names and returns sorted partition with no unknown names."""
    instances = {"survival": _inst("survival"), "creative": _inst("creative")}
    partition, unknown = resolve_partition(instances, ["survival", "survival", "creative", "creative"])
    assert partition == ["creative", "survival"]
    assert unknown == []


def test_resolve_partition_empty_set_is_empty() -> None:
    """Verifies resolving an empty requested instance set yields empty partition and unknown lists."""
    instances = {"survival": _inst("survival")}
    partition, unknown = resolve_partition(instances, set())
    assert partition == []
    assert unknown == []


def test_resolve_compose_path_relative(tmp_path: Path) -> None:
    """Relative compose source paths resolve against the compose base dir."""
    base = tmp_path / "compose_dir"
    base.mkdir()
    resolved = resolve_compose_path(Path("./data"), base)
    assert resolved == (base / "data").resolve()


def test_resolve_compose_path_absolute_kept(tmp_path: Path) -> None:
    """Absolute compose source paths are returned resolved."""
    abs_path = (tmp_path / "abs").resolve()
    abs_path.mkdir()
    assert resolve_compose_path(abs_path, tmp_path) == abs_path


def _write_full_repo(tmp_path: Path) -> Path:
    """Create a minimal but valid project root. Returns config_dir."""
    project_root = tmp_path
    config_dir = project_root / "config.d"
    config_dir.mkdir()
    (project_root / "docker-compose.yml").write_text(
        '\nservices:\n  mc-survival:\n    container_name: mc-survival\n    healthcheck:\n      test: ["CMD", "true"]\n    volumes:\n      - ./data/survival:/data\n      - ./shared/mods:/data/mods\n    stop_grace_period: 30s\n  nginx:\n    container_name: nginx\n    volumes:\n      - ./www:/usr/share/nginx/html\nsecrets:\n  rcon_password:\n    file: ./secrets/rcon.txt\n',
        encoding="utf-8",
    )
    (config_dir / "deploy_pack.toml").write_text(
        '\ninstance_discovery = "explicit"\nsync_root    = "./sync"\nmodpack_dir  = "./sync/downloads"\noutput_filename   = "minecraft_client_{date}.zip"\ndownload_base_url = "http://minecraft/downloads"\nprotect_file = "./.deploy_protect"\n\n[sync_mapping]\nconfig = "config"\nkubejs = "kubejs"\n\n[docker]\ncompose_file = "./docker-compose.yml"\n\n[instances.survival]\ncontainer = "mc-survival"\nconfig_mode = "merge"\nkubejs_mode = "delete"\n\n[resource_pack.survival]\nfilename = "pack.zip"\nrequired = true\nprompt   = ""\n',
        encoding="utf-8",
    )
    return config_dir


def test_load_deployment_config_minimal(tmp_path: Path) -> None:
    """Tests loading a deployment config with only the required minimal setup."""
    config_dir = _write_full_repo(tmp_path)
    cfg = load_deployment_config(config_dir)
    assert cfg.project_root == tmp_path
    assert cfg.config_dir == config_dir
    assert cfg.sync_root == (tmp_path / "sync").resolve()
    assert cfg.modpack_dir == (tmp_path / "sync" / "downloads").resolve()
    assert cfg.www_dir == (tmp_path / "www").resolve()
    assert cfg.www_dir_error is None
    assert cfg.output_filename == "minecraft_client_{date}.zip"
    assert cfg.download_base_url == "http://minecraft/downloads"
    assert cfg.protect_file == (tmp_path / ".deploy_protect").resolve()
    assert cfg.compose.ok
    assert "survival" in cfg.instances
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
    assert cfg.resource_packs["survival"].required is True
    assert cfg.resource_packs["survival"].prompt == ""
    assert cfg.partition == ["survival"]
    assert cfg.partition_unknown == []
    assert cfg.requested_instances is None
    assert cfg.partition_requested is False


def test_load_deployment_config_partition_with_unknown(tmp_path: Path) -> None:
    """Tests partition resolution when requested instances include unknown names."""
    config_dir = _write_full_repo(tmp_path)
    cfg = load_deployment_config(config_dir, requested_instances={"survival", "nope"})
    assert cfg.partition == ["survival"]
    assert cfg.partition_unknown == ["nope"]
    assert cfg.requested_instances == {"survival", "nope"}
    assert cfg.partition_requested is True


def test_load_deployment_config_instance_match_error_recorded(tmp_path: Path) -> None:
    """Tests that config load records an instance match error on the InstanceConfig.

    §3.6: no match is a partition-scoped failure. Config load records it on
    the InstanceConfig; preflight decides.
    """
    config_dir = _write_full_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace('container = "mc-survival"', 'container = "mc-nope"'), encoding="utf-8")
    cfg = load_deployment_config(config_dir)
    inst = cfg.instances["survival"]
    assert inst.service is None
    assert inst.service_match_error is not None
    assert inst.instance_root is None


def test_load_deployment_config_stop_grace_error_recorded(tmp_path: Path) -> None:
    """Tests that config load records an unparseable stop_grace_period.

    §3.2: unparseable stop_grace_period is a partition-scoped failure. Config
    load records it; preflight decides.
    """
    config_dir = _write_full_repo(tmp_path)
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(compose.read_text(encoding="utf-8").replace("stop_grace_period: 30s", "stop_grace_period: 1.5s"), encoding="utf-8")
    cfg = load_deployment_config(config_dir)
    inst = cfg.instances["survival"]
    assert inst.stop_grace_period_raw == "1.5s"
    assert inst.stop_grace_parse_error is not None
    assert inst.stop_grace_seconds == 10


def test_load_deployment_config_broken_compose_does_not_raise(tmp_path: Path) -> None:
    """§3.5, §4.3: broken compose must not short-circuit config load."""
    config_dir = _write_full_repo(tmp_path)
    (tmp_path / "docker-compose.yml").write_text(":::not yaml", encoding="utf-8")
    cfg = load_deployment_config(config_dir)
    assert not cfg.compose.ok
    assert cfg.compose.error is not None
    assert cfg.www_dir is None
    assert cfg.www_dir_error is not None
    inst = cfg.instances["survival"]
    assert inst.service is None
    assert inst.instance_root is None


def test_load_deployment_config_www_dir_toml_survives_broken_compose(tmp_path: Path) -> None:
    """TOML www_dir wins regardless of compose state (§3.4, §3.5)."""
    config_dir = _write_full_repo(tmp_path)
    (tmp_path / "docker-compose.yml").write_text(":::not yaml", encoding="utf-8")
    toml = config_dir / "deploy_pack.toml"
    toml.write_text('www_dir = "./override_www"\n' + toml.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_deployment_config(config_dir)
    assert cfg.www_dir == (tmp_path / "override_www").resolve()
    assert cfg.www_dir_error is None


def test_load_deployment_config_www_dir_undeterminable(tmp_path: Path) -> None:
    """Compose present but no /usr/share/nginx/ bind → diagnostic, not raise."""
    config_dir = _write_full_repo(tmp_path)
    (tmp_path / "docker-compose.yml").write_text(
        '\nservices:\n  mc-survival:\n    container_name: mc-survival\n    healthcheck:\n      test: ["CMD", "true"]\n    volumes:\n      - ./data/survival:/data\n      - ./shared/mods:/data/mods\n',
        encoding="utf-8",
    )
    cfg = load_deployment_config(config_dir)
    assert cfg.compose.ok
    assert cfg.www_dir is None
    assert cfg.www_dir_error is not None


def test_env_file_loads_flat_keys(tmp_path: Path) -> None:
    """config.d/.env supplies flat keys, used verbatim (§3.12)."""
    config_dir = _write_full_repo(tmp_path)
    (config_dir / ".env").write_text("webhook_url=https://discord.example/webhook\n", encoding="utf-8")
    cfg = load_deployment_config(config_dir)
    assert cfg.webhook_url == "https://discord.example/webhook"


def test_env_file_does_not_override_toml(tmp_path: Path) -> None:
    """TOML has higher priority than .env for the same key (§3.1)."""
    config_dir = _write_full_repo(tmp_path)
    (config_dir / ".env").write_text("output_filename=from_env.zip\n", encoding="utf-8")
    cfg = load_deployment_config(config_dir)
    assert cfg.output_filename == "minecraft_client_{date}.zip"


def test_missing_env_file_is_silent(tmp_path: Path) -> None:
    """Absence of config.d/.env is not an error (§3.12)."""
    config_dir = _write_full_repo(tmp_path)
    assert not (config_dir / ".env").exists()
    cfg = load_deployment_config(config_dir)
    assert cfg.webhook_url is None


def test_env_file_keys_are_case_sensitive(tmp_path: Path) -> None:
    """Keys are used verbatim - no case folding (§3.12)."""
    config_dir = _write_full_repo(tmp_path)
    (config_dir / ".env").write_text("WEBHOOK_URL=https://discord.example/webhook\n", encoding="utf-8")
    cfg = load_deployment_config(config_dir)
    assert cfg.webhook_url is None


def test_env_file_malformed_lines_skipped(tmp_path: Path) -> None:
    """ConfigCore.load_env silently skips lines without '=' (§3.12).

    A malformed .env must not be a fatal error: the spec (§3.12) wants
    a missing/malformed webhook to produce a warning, not an exit 3.
    Valid keys around the malformed line still load.
    """
    config_dir = _write_full_repo(tmp_path)
    (config_dir / ".env").write_text(
        "# a comment\n\nwebhook_url=https://discord.example/webhook\nthis line has no equals sign\nanother_url=https://example\n", encoding="utf-8"
    )
    cfg = load_deployment_config(config_dir)
    assert cfg.webhook_url == "https://discord.example/webhook"


def test_cli_overlay_overrides_toml(tmp_path: Path) -> None:
    """Verifies that CLI options override values loaded from the TOML config."""
    config_dir = _write_full_repo(tmp_path)
    cfg = load_deployment_config(config_dir, cli_remaining=["--output-filename", "custom_{date}.zip"])
    assert cfg.output_filename == "custom_{date}.zip"


def test_cli_overlay_equals_form(tmp_path: Path) -> None:
    """Tests that a CLI --download-base-url value in equals form overrides the loaded config."""
    config_dir = _write_full_repo(tmp_path)
    cfg = load_deployment_config(config_dir, cli_remaining=["--download-base-url=http://other/"])
    assert cfg.download_base_url == "http://other/"


def test_load_deployment_config_empty_download_base_url(tmp_path: Path) -> None:
    """Tests that loading a deployment config with an empty download base URL raises ConfigError."""
    config_dir = _write_full_repo(tmp_path)
    with pytest.raises(ConfigError):
        load_deployment_config(config_dir, cli_remaining=["--download-base-url="])


def test_load_deployment_config_orphan_resource_pack(tmp_path: Path) -> None:
    """Tests that loading a deployment config with an orphaned resource pack section raises ConfigError."""
    config_dir = _write_full_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + '\n\n[resource_pack.orphan]\nfilename = "x.zip"\nrequired = false\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_deployment_config(config_dir)


def test_load_deployment_config_bad_output_filename(tmp_path: Path) -> None:
    """Tests that loading a deployment config with an invalid output filename extension raises ConfigError."""
    config_dir = _write_full_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace('output_filename   = "minecraft_client_{date}.zip"', 'output_filename   = "pack.tar.gz"'), encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_deployment_config(config_dir)


def test_load_deployment_config_negative_timing(tmp_path: Path) -> None:
    """Tests that loading a deployment config with a negative restart wait time raises ConfigError."""
    config_dir = _write_full_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            '[docker]\ncompose_file = "./docker-compose.yml"', '[docker]\ncompose_file = "./docker-compose.yml"\nrestart_wait_seconds = -1'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_deployment_config(config_dir)


def test_load_deployment_config_zero_health_poll(tmp_path: Path) -> None:
    """Tests that loading a deployment config raises ConfigError when health_poll_seconds is zero."""
    config_dir = _write_full_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            '[docker]\ncompose_file = "./docker-compose.yml"', '[docker]\ncompose_file = "./docker-compose.yml"\nhealth_poll_seconds = 0'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_deployment_config(config_dir)


def test_load_deployment_config_missing_required_rp_key(tmp_path: Path) -> None:
    """Tests that loading a deployment config raises ConfigError when a required rp key is missing."""
    config_dir = _write_full_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace("required = true", ""), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_deployment_config(config_dir)
