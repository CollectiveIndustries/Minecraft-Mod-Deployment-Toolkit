# tests/unit/deploy_pack/test_deploy_pack.py

"""Unit tests for deploy_pack.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.minecraft import deploy_pack
from src.minecraft.common import changelog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_logger():
    """Return a MagicMock logger suitable for any deploy_pack call."""
    return MagicMock()


def _config_getter(base: Path):
    """Return a config.get side_effect wired to a tmp base directory.

    Every path the deploy reads or writes is redirected under ``base``
    so tests never touch the real filesystem outside tmp_path.
    """
    mapping = {
        "sync_root": str(base / "sync"),
        "mods_dir": str(base / "mods"),
        "www_dir": str(base / "www"),
        "exclude_file": str(base / ".rsync_exclude"),
        "protect_file": str(base / ".deploy_protect"),
        "modpack_dir": str(base / "sync" / "downloads"),
        "output_filename": "minecraft_client_{date}.zip",
        "instances": {"survival": str(base / "survival")},
        "multimc_base": str(base / "multimc"),
        "instance_name": "TestInstance",
        "sync_mapping": {"config": "config", "kubejs": "kubejs"},
        "logging": None,
        "download_base_url": "",
        "webhook_url": "",
        "webhook_message_template": "{url}",
    }

    def getter(key, default=None):
        return mapping.get(key, default)

    return getter


def _make_main_config(base: Path) -> MagicMock:
    """Build a MagicMock config that routes all paths under ``base``."""
    cfg = MagicMock()
    cfg.get.side_effect = _config_getter(base)
    cfg.as_dict.return_value = {"sync_root": str(base / "sync")}
    return cfg


@pytest.fixture
def patch_dependencies(tmp_path):
    """Patch module-level dependencies used by main().

    Yields a dict of the notable mocks so tests can assert on calls
    and set side effects. Every external path is redirected under
    tmp_path via _config_getter.
    """
    mock_load_config = MagicMock()
    mock_load_config.return_value = _make_main_config(tmp_path)

    with (
        patch("src.minecraft.deploy_pack.cfg.load_config", mock_load_config),
        patch("src.minecraft.deploy_pack.get_logger", return_value=MagicMock()),
        patch("src.minecraft.deploy_pack.setup_logging"),
        patch("src.minecraft.deploy_pack.load_mod_list", return_value=[]) as mock_load_mod_list,
        patch(
            "src.minecraft.deploy_pack.prepare_mods_staging",
            return_value=tmp_path / "staging_mods",
        ) as mock_prepare_mods,
        patch(
            "src.minecraft.deploy_pack.prepare_instance_staging",
            return_value=tmp_path / "staging_inst",
        ) as mock_prepare_instance,
        patch(
            "src.minecraft.deploy_pack.prepare_client_staging",
            return_value=tmp_path / "staging_client",
        ) as mock_prepare_client,
        patch(
            "src.minecraft.deploy_pack.create_client_zip",
            return_value=tmp_path / "www" / "minecraft_client_20260920.zip",
        ) as mock_create_zip,
        patch("src.minecraft.deploy_pack.publish_release") as mock_publish,
        patch("src.minecraft.deploy_pack.deploy_to_server") as mock_deploy_server,
        patch("src.minecraft.deploy_pack.deploy_to_client") as mock_deploy_client,
        patch("src.minecraft.deploy_pack.deploy_shared_items") as mock_shared,
        patch(
            "src.minecraft.deploy_pack.file_utils.get_exclude_patterns",
            return_value=[],
        ),
        patch(
            "src.minecraft.deploy_pack.file_utils.get_protect_patterns",
            return_value=["server.properties"],
        ),
    ):
        yield {
            "mock_load_config": mock_load_config,
            "mock_load_mod_list": mock_load_mod_list,
            "mock_prepare_mods": mock_prepare_mods,
            "mock_prepare_instance": mock_prepare_instance,
            "mock_prepare_client": mock_prepare_client,
            "mock_create_zip": mock_create_zip,
            "mock_publish": mock_publish,
            "mock_deploy_server": mock_deploy_server,
            "mock_deploy_client": mock_deploy_client,
            "mock_shared": mock_shared,
        }


def _stub_diff_report() -> changelog.DiffReport:
    """Return a minimal populated DiffReport for publish_release tests."""
    return changelog.DiffReport(added_mods=["a.jar"], added_kubejs=["x.js"])


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestResolveMappingForSide:
    """Tests for resolve_mapping_for_side behavior across mapping shapes."""

    def test_string_applies_to_both_sides(self):
        """A bare string destination is returned for either side."""
        assert deploy_pack.resolve_mapping_for_side("config", "server") == "config"
        assert deploy_pack.resolve_mapping_for_side("config", "client") == "config"

    def test_dict_picks_per_side(self):
        """A dict destination returns the entry for the requested side."""
        val = {"server": "srv/cfg", "client": "cli/cfg"}
        assert deploy_pack.resolve_mapping_for_side(val, "server") == "srv/cfg"
        assert deploy_pack.resolve_mapping_for_side(val, "client") == "cli/cfg"

    def test_minus_one_excludes_side(self):
        """A value of -1 marks the side as excluded."""
        val = {"server": -1, "client": "somewhere"}
        assert deploy_pack.resolve_mapping_for_side(val, "server") is None
        assert deploy_pack.resolve_mapping_for_side(val, "client") == "somewhere"

    def test_missing_side_returns_none(self):
        """A dict without the requested side key returns None."""
        val = {"server": "srv"}
        assert deploy_pack.resolve_mapping_for_side(val, "client") is None

    def test_non_string_value_returns_none(self):
        """A non-string, non-dict value returns None."""
        val = {"server": 42}
        assert deploy_pack.resolve_mapping_for_side(val, "server") is None


class TestIsSharedDest:
    """Tests for is_shared_dest detection of shared destination paths."""

    def test_at_prefix_is_shared(self):
        """Any destination starting with '@' is shared."""
        assert deploy_pack.is_shared_dest("@www")
        assert deploy_pack.is_shared_dest("@www/foo")

    def test_plain_path_is_not_shared(self):
        """A plain relative path is not shared."""
        assert not deploy_pack.is_shared_dest("config")
        assert not deploy_pack.is_shared_dest("server/config")


class TestResolveSharedDest:
    """Tests for resolve_shared_dest expansion of @-prefixed paths."""

    def test_www_root(self, tmp_path):
        """'@www' resolves to the www directory itself."""
        assert deploy_pack.resolve_shared_dest("@www", tmp_path / "www", tmp_path / "mods") == tmp_path / "www"

    def test_www_subpath(self, tmp_path):
        """'@www/sub' resolves to a subdirectory of the www directory."""
        assert deploy_pack.resolve_shared_dest("@www/resourcepacks", tmp_path / "www", tmp_path / "mods") == tmp_path / "www" / "resourcepacks"

    def test_mods_root(self, tmp_path):
        """'@mods' resolves to the mods directory itself."""
        assert deploy_pack.resolve_shared_dest("@mods", tmp_path / "www", tmp_path / "mods") == tmp_path / "mods"

    def test_mods_subpath(self, tmp_path):
        """'@mods/sub' resolves to a subdirectory of the mods directory."""
        assert deploy_pack.resolve_shared_dest("@mods/extra", tmp_path / "www", tmp_path / "mods") == tmp_path / "mods" / "extra"

    def test_unknown_prefix_raises(self, tmp_path):
        """An unknown @-prefix raises ValueError."""
        with pytest.raises(ValueError):
            deploy_pack.resolve_shared_dest("@nope/foo", tmp_path / "www", tmp_path / "mods")


# ---------------------------------------------------------------------------
# Deployment actions
# ---------------------------------------------------------------------------


def test_deploy_to_server(tmp_path, mock_logger):
    """deploy_to_server calls copy_with_exclusions with clean and protect patterns."""
    with patch("src.minecraft.deploy_pack.file_utils.copy_with_exclusions") as mock_copy:
        deploy_pack.deploy_to_server(
            staging_dir=tmp_path / "s",
            live_server=tmp_path / "srv",
            exclude_patterns=["*.tmp"],
            protect_patterns=["server.properties"],
            logger=mock_logger,
        )
    mock_copy.assert_called_once()
    _, kwargs = mock_copy.call_args
    assert kwargs["clean"] is True
    assert kwargs["protect_patterns"] == ["server.properties"]


def test_deploy_to_client(tmp_path, mock_logger):
    """deploy_to_client targets <base>/<name>/.minecraft."""
    with patch("src.minecraft.deploy_pack.file_utils.copy_with_exclusions") as mock_copy:
        deploy_pack.deploy_to_client(
            staging_dir=tmp_path / "s",
            multimc_base=tmp_path / "mmc",
            instance_name="Inst",
            exclude_patterns=[],
            logger=mock_logger,
        )
    args, _ = mock_copy.call_args
    assert args[1] == tmp_path / "mmc" / "Inst" / ".minecraft"


# ---------------------------------------------------------------------------
# create_client_zip
# ---------------------------------------------------------------------------


def test_create_client_zip_uses_output_dir(tmp_path, mock_logger):
    """create_client_zip writes into the caller-supplied output_dir."""
    output_dir = tmp_path / "www" / "dry_run"
    with patch("src.minecraft.deploy_pack.file_utils.create_zip_from_staging") as mock_zip:
        result = deploy_pack.create_client_zip(
            staging_dir=tmp_path / "staging",
            output_dir=output_dir,
            filename_template="pack_{date}.zip",
            exclude_patterns=[],
            logger=mock_logger,
        )
    assert result.parent == output_dir
    assert result.name.startswith("pack_")
    mock_zip.assert_called_once()


# ---------------------------------------------------------------------------
# publish_release - the four notification modes
# ---------------------------------------------------------------------------


def test_publish_release_writes_html_and_skips_notify(tmp_path, mock_logger):
    """--no-notify: HTML written, no webhook call."""
    cfg = _make_main_config(tmp_path)
    with (
        patch("src.minecraft.common.changelog.write_changelog") as mock_html,
        patch("src.minecraft.deploy_pack.notify.post_discord_webhook") as mock_webhook,
    ):
        deploy_pack.publish_release(
            client_zip=tmp_path / "www" / "pack.zip",
            report=_stub_diff_report(),
            config=cfg,
            output_dir=tmp_path / "www",
            logger=mock_logger,
            no_notify=True,
        )
    mock_html.assert_called_once()
    mock_webhook.assert_not_called()


def test_publish_release_dry_run_prints_payload(tmp_path, mock_logger):
    """--dry-run: HTML written, payload logged, no webhook call."""
    cfg = _make_main_config(tmp_path)
    with (
        patch("src.minecraft.common.changelog.write_changelog") as mock_html,
        patch("src.minecraft.deploy_pack.notify.post_discord_webhook") as mock_webhook,
    ):
        deploy_pack.publish_release(
            client_zip=tmp_path / "www" / "pack.zip",
            report=_stub_diff_report(),
            config=cfg,
            output_dir=tmp_path / "www" / "dry_run",
            logger=mock_logger,
            dry_run=True,
        )
    mock_html.assert_called_once()
    mock_webhook.assert_not_called()
    logged = " ".join(str(c) for c in mock_logger.info.call_args_list)
    assert "DRY-RUN" in logged


def test_publish_release_dry_run_notify_posts(tmp_path, mock_logger):
    """--dry-run-notify: HTML written, webhook actually sent."""
    cfg = _make_main_config(tmp_path)
    cfg.get.side_effect = lambda key, default=None: {
        "download_base_url": "https://x.test/p",
        "webhook_url": "https://discord.test/hook",
        "webhook_message_template": "{url}",
    }.get(key, default)
    with (
        patch("src.minecraft.common.changelog.write_changelog"),
        patch("src.minecraft.deploy_pack.file_utils.compute_file_hash", return_value="x"),
        patch(
            "src.minecraft.deploy_pack.notify.post_discord_webhook",
            return_value=True,
        ) as mock_webhook,
    ):
        deploy_pack.publish_release(
            client_zip=tmp_path / "www" / "pack.zip",
            report=_stub_diff_report(),
            config=cfg,
            output_dir=tmp_path / "www" / "dry_run",
            logger=mock_logger,
            dry_run=True,
            dry_run_notify=True,
        )
    mock_webhook.assert_called_once()


def test_publish_release_normal_deploy_posts(tmp_path, mock_logger):
    """Normal deploy: HTML written, webhook sent when configured."""
    cfg = _make_main_config(tmp_path)
    cfg.get.side_effect = lambda key, default=None: {
        "download_base_url": "https://example.test/packs",
        "webhook_url": "https://discord.test/hook",
        "webhook_message_template": "New pack: {url} sha {sha256sum}",
    }.get(key, default)
    with (
        patch("src.minecraft.common.changelog.write_changelog"),
        patch("src.minecraft.deploy_pack.file_utils.compute_file_hash", return_value="deadbeef"),
        patch(
            "src.minecraft.deploy_pack.notify.post_discord_webhook",
            return_value=True,
        ) as mock_webhook,
    ):
        deploy_pack.publish_release(
            client_zip=tmp_path / "www" / "pack.zip",
            report=_stub_diff_report(),
            config=cfg,
            output_dir=tmp_path / "www",
            logger=mock_logger,
        )
    mock_webhook.assert_called_once()
    args, _ = mock_webhook.call_args
    assert "deadbeef" in args[1]
    assert "https://example.test/packs/pack.zip" in args[1]


def test_publish_release_no_webhook_url_logs_and_returns(tmp_path, mock_logger):
    """No webhook_url configured: HTML written, no webhook attempt."""
    cfg = _make_main_config(tmp_path)
    cfg.get.side_effect = lambda key, default=None: {
        "download_base_url": "",
        "webhook_url": "",
        "webhook_message_template": "{url}",
    }.get(key, default)
    with (
        patch("src.minecraft.common.changelog.write_changelog"),
        patch("src.minecraft.deploy_pack.file_utils.compute_file_hash", return_value="x"),
        patch("src.minecraft.deploy_pack.notify.post_discord_webhook") as mock_webhook,
    ):
        deploy_pack.publish_release(
            client_zip=tmp_path / "www" / "pack.zip",
            report=_stub_diff_report(),
            config=cfg,
            output_dir=tmp_path / "www",
            logger=mock_logger,
        )
    mock_webhook.assert_not_called()


def test_publish_release_bad_template_falls_back(tmp_path, mock_logger):
    """A typo in the template must not raise; falls back to minimal message."""
    cfg = _make_main_config(tmp_path)
    cfg.get.side_effect = lambda key, default=None: {
        "download_base_url": "https://x.test/p",
        "webhook_url": "https://discord.test/hook",
        "webhook_message_template": "oops {nonexistent}",
    }.get(key, default)
    with (
        patch("src.minecraft.common.changelog.write_changelog"),
        patch("src.minecraft.deploy_pack.file_utils.compute_file_hash", return_value="x"),
        patch(
            "src.minecraft.deploy_pack.notify.post_discord_webhook",
            return_value=True,
        ) as mock_webhook,
    ):
        deploy_pack.publish_release(
            client_zip=tmp_path / "www" / "pack.zip",
            report=_stub_diff_report(),
            config=cfg,
            output_dir=tmp_path / "www",
            logger=mock_logger,
        )
    mock_webhook.assert_called_once()


# ---------------------------------------------------------------------------
# load_instances
# ---------------------------------------------------------------------------


def test_load_instances_empty(mock_logger):
    """No instances key returns an empty list."""
    cfg = MagicMock()
    cfg.get.return_value = None
    assert deploy_pack.load_instances(cfg, mock_logger) == []


def test_load_instances_dict_form(mock_logger):
    """The [instances.<name>] table form is parsed to (name, path) pairs."""
    cfg = MagicMock()
    cfg.get.return_value = {"survival": {"path": "./survival"}}
    result = deploy_pack.load_instances(cfg, mock_logger)
    assert result == [("survival", Path("./survival"))]


def test_load_instances_scalar_form(mock_logger):
    """The instances = { name = "path" } form is parsed to (name, path) pairs."""
    cfg = MagicMock()
    cfg.get.return_value = {"survival": "./survival"}
    result = deploy_pack.load_instances(cfg, mock_logger)
    assert result == [("survival", Path("./survival"))]


def test_load_instances_missing_path_skips(mock_logger):
    """An instance with no path is skipped with a warning."""
    cfg = MagicMock()
    cfg.get.return_value = {"survival": {}, "creative": "./creative"}
    result = deploy_pack.load_instances(cfg, mock_logger)
    assert result == [("creative", Path("./creative"))]


# ---------------------------------------------------------------------------
# main() - end-to-end smoke tests
# ---------------------------------------------------------------------------


def test_main_server_default(patch_dependencies):
    """Default server mode: mods, instances, shared, ZIP, publish."""
    with patch("sys.argv", ["deploy_pack.py"]):
        deploy_pack.main()

    mocks = patch_dependencies
    mocks["mock_prepare_mods"].assert_called_once()
    mocks["mock_prepare_instance"].assert_called_once()
    mocks["mock_shared"].assert_called_once()
    mocks["mock_create_zip"].assert_called_once()
    mocks["mock_publish"].assert_called_once()


def test_main_server_no_deploy(patch_dependencies):
    """--no-deploy: skip mods/instances/shared, still build ZIP + publish."""
    with patch("sys.argv", ["deploy_pack.py", "--no-deploy"]):
        deploy_pack.main()

    mocks = patch_dependencies
    mocks["mock_prepare_mods"].assert_not_called()
    mocks["mock_prepare_instance"].assert_not_called()
    mocks["mock_shared"].assert_not_called()
    mocks["mock_create_zip"].assert_called_once()
    mocks["mock_publish"].assert_called_once()


def test_main_server_no_zip(patch_dependencies):
    """--no-zip: skip ZIP + publish, still deploy server side."""
    with patch("sys.argv", ["deploy_pack.py", "--no-zip"]):
        deploy_pack.main()

    mocks = patch_dependencies
    mocks["mock_prepare_mods"].assert_called_once()
    mocks["mock_prepare_instance"].assert_called_once()
    mocks["mock_shared"].assert_called_once()
    mocks["mock_create_zip"].assert_not_called()
    mocks["mock_publish"].assert_not_called()


def test_main_server_dry_run(patch_dependencies):
    """--dry-run implies --no-deploy: no server writes, still build ZIP + publish."""
    with patch("sys.argv", ["deploy_pack.py", "--dry-run"]):
        deploy_pack.main()

    mocks = patch_dependencies
    mocks["mock_prepare_mods"].assert_not_called()
    mocks["mock_prepare_instance"].assert_not_called()
    mocks["mock_shared"].assert_not_called()
    mocks["mock_create_zip"].assert_called_once()
    mocks["mock_publish"].assert_called_once()
    _, kwargs = mocks["mock_publish"].call_args
    assert kwargs["dry_run"] is True
    assert kwargs["dry_run_notify"] is False
    assert kwargs["no_notify"] is False


def test_main_server_dry_run_notify(patch_dependencies):
    """--dry-run-notify is passed through to publish_release."""
    with patch("sys.argv", ["deploy_pack.py", "--dry-run", "--dry-run-notify"]):
        deploy_pack.main()

    _, kwargs = patch_dependencies["mock_publish"].call_args
    assert kwargs["dry_run"] is True
    assert kwargs["dry_run_notify"] is True


def test_main_server_no_notify(patch_dependencies):
    """--no-notify is passed through to publish_release."""
    with patch("sys.argv", ["deploy_pack.py", "--no-notify"]):
        deploy_pack.main()

    _, kwargs = patch_dependencies["mock_publish"].call_args
    assert kwargs["no_notify"] is True
    assert kwargs["dry_run"] is False


def test_main_dry_run_notify_requires_dry_run(patch_dependencies):
    """--dry-run-notify without --dry-run is a parser error."""
    with (
        patch("sys.argv", ["deploy_pack.py", "--dry-run-notify"]),
        pytest.raises(SystemExit),
    ):
        deploy_pack.main()


def test_main_client_mode(patch_dependencies):
    """Client mode: build client staging and deploy to MultiMC."""
    with patch("sys.argv", ["deploy_pack.py", "--client"]):
        deploy_pack.main()

    mocks = patch_dependencies
    mocks["mock_prepare_client"].assert_called_once()
    mocks["mock_deploy_client"].assert_called_once()
    mocks["mock_create_zip"].assert_not_called()


def test_main_config_dir_file(patch_dependencies):
    """--config-dir pointing at a file uses its parent dir."""
    with (
        patch("sys.argv", ["deploy_pack.py", "--config-dir", "/some/path/file.toml"]),
        patch("pathlib.Path.is_file", return_value=True),
    ):
        deploy_pack.main()


def test_main_debug(patch_dependencies):
    """--debug prints the config dict without crashing."""
    with (
        patch("sys.argv", ["deploy_pack.py", "--debug"]),
        patch("builtins.print"),
    ):
        deploy_pack.main()


def test_main_load_mods_failure(patch_dependencies):
    """If load_mod_list raises, exit 1."""
    patch_dependencies["mock_load_mod_list"].side_effect = ValueError("Bad index")
    with (
        patch("sys.argv", ["deploy_pack.py"]),
        pytest.raises(SystemExit) as exc,
    ):
        deploy_pack.main()
    assert exc.value.code == 1


def test_main_prepare_staging_failure(patch_dependencies):
    """If a staging step raises, exit 1."""
    patch_dependencies["mock_prepare_mods"].side_effect = OSError("Disk full")
    with (
        patch("sys.argv", ["deploy_pack.py"]),
        pytest.raises(SystemExit) as exc,
    ):
        deploy_pack.main()
    assert exc.value.code == 1


def test_main_no_instances_and_no_deploy(patch_dependencies, tmp_path):
    """--no-deploy with no instances defined is allowed."""
    patch_dependencies["mock_load_config"].return_value.get.side_effect = lambda key, default=None: (
        None if key == "instances" else _config_getter(tmp_path)(key, default)
    )
    with patch("sys.argv", ["deploy_pack.py", "--no-deploy"]):
        deploy_pack.main()
