"""Unit tests for deploy_pack.py."""

import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.minecraft import deploy_pack


@pytest.fixture
def mock_logger():
    """Creates a mock logger for testing.

    Returns:
        MagicMock: A mock logger instance that can be used to verify logging calls.
    """
    logger = MagicMock()
    return logger


@pytest.fixture
def mock_config():
    """Creates a mock configuration object for testing.

    Returns:
        MagicMock: A mock config with predefined values for sync_root, live_server,
            www_dir, exclude_file, output_filename, modpack_dir, sync_mapping,
            multimc_base, instance_name, and logging level. The mock also provides
            a get() method with side_effect returning the configured values and an
            empty as_dict().
    """
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "sync_root": "/fake/sync",
        "live_server": "/fake/server",
        "www_dir": "/fake/www",
        "exclude_file": "/fake/sync/.rsync_exclude",
        "output_filename": "test_{date}.zip",
        "modpack_dir": "/fake/sync/downloads",
        "sync_mapping": {
            "config": "config",
            "kubejs": "kubejs",
            "resourcepacks": {"server": "www/resourcepacks", "client": "resourcepacks"},
            "server_only": {"server": "server_stuff", "client": -1},
            "client_only": {"client": "client_stuff", "server": -1},
        },
        "multimc_base": "/fake/multimc",
        "instance_name": "TestInstance",
        "logging": {"level": "INFO"},
    }.get(key, default)
    config.as_dict.return_value = {}
    return config


@pytest.fixture
def patch_dependencies():
    """Common patches for deploy_pack.main tests."""
    with (
        patch("src.minecraft.deploy_pack.cfg.load_config") as mock_load_config,
        patch("src.minecraft.deploy_pack.setup_logging"),
        patch("src.minecraft.deploy_pack.get_logger") as mock_get_logger,
        patch("src.minecraft.deploy_pack.file_utils.get_exclude_patterns") as mock_get_exclude,
        patch("src.minecraft.deploy_pack.load_mod_list") as mock_load_mod_list,
        patch("src.minecraft.deploy_pack.prepare_staging") as mock_prepare_staging,
        patch("src.minecraft.deploy_pack.create_client_zip") as mock_create_zip,
        patch("src.minecraft.deploy_pack.deploy_to_server") as mock_deploy_server,
        patch("src.minecraft.deploy_pack.deploy_to_client") as mock_deploy_client,
        patch("src.minecraft.deploy_pack.tempfile.mkdtemp") as mock_mkdtemp,
        patch("src.minecraft.deploy_pack.shutil.rmtree") as mock_rmtree,
    ):
        logger = MagicMock()
        mock_get_logger.return_value = logger
        mock_get_exclude.return_value = ["*.tmp", "*.bak"]
        mock_load_mod_list.return_value = [{"file": "mod1.jar"}, {"file": "mod2.jar"}]
        mock_prepare_staging.return_value = Path("/fake/staging")
        yield {
            "mock_load_config": mock_load_config,
            "mock_logger": logger,
            "mock_get_exclude": mock_get_exclude,
            "mock_load_mod_list": mock_load_mod_list,
            "mock_prepare_staging": mock_prepare_staging,
            "mock_create_zip": mock_create_zip,
            "mock_deploy_server": mock_deploy_server,
            "mock_deploy_client": mock_deploy_client,
            "mock_mkdtemp": mock_mkdtemp,
            "mock_rmtree": mock_rmtree,
        }


def test_load_mod_list_success(mock_logger):
    """Tests successful loading of mod list with all dependencies mocked.

    Args:
        mock_logger: Mock logger fixture.

    Verifies that the function correctly loads the Prism index, applies side
    overrides, filters by target side, and logs appropriate messages.
    """
    prism_index = Path("/fake/.index")
    config_dir = Path("/fake/config")
    target_side = "server"
    with (
        patch("src.minecraft.deploy_pack.prism.load_prism_index") as mock_load,
        patch("src.minecraft.deploy_pack.overrides.load_side_overrides") as mock_load_overrides,
        patch("src.minecraft.deploy_pack.overrides.apply_side_overrides") as mock_apply,
        patch("src.minecraft.deploy_pack.prism.filter_prism_entries_by_side") as mock_filter,
        patch("pathlib.Path.is_dir") as mock_is_dir,
    ):
        mock_is_dir.return_value = True
        mock_load.return_value = [{"id": "1", "side": "both"}]
        mock_load_overrides.return_value = {"1": "server"}
        mock_apply.return_value = [{"id": "1", "side": "server"}]
        mock_filter.return_value = [{"id": "1", "side": "server"}]
        result = deploy_pack.load_mod_list(prism_index, config_dir, target_side, mock_logger)
        assert len(result) == 1
        mock_load.assert_called_once_with(prism_index)
        mock_load_overrides.assert_called_once_with(config_dir / "side_overrides.toml")
        mock_apply.assert_called_once()
        mock_filter.assert_called_once_with(mock_apply.return_value, target_side)
        mock_logger.info.assert_any_call("Loaded 1 mods from Prism index")
        mock_logger.info.assert_any_call("Filtered to 1 mods for side 'server'")


def test_load_mod_list_index_missing(mock_logger):
    """Tests load_mod_list when the Prism index directory does not exist.

    Args:
        mock_logger: Mock logger fixture.

    Verifies that ValueError is raised and load_prism_index is not called.
    """
    prism_index = Path("/fake/.index")
    with patch("src.minecraft.deploy_pack.prism.load_prism_index") as mock_load, patch("pathlib.Path.is_dir") as mock_is_dir:
        mock_is_dir.return_value = False
        with pytest.raises(ValueError, match="Prism index directory not found"):
            deploy_pack.load_mod_list(prism_index, Path("/fake"), "server", mock_logger)
        mock_load.assert_not_called()


def test_load_mod_list_empty(mock_logger):
    """Tests load_mod_list when the Prism index returns an empty list.

    Args:
        mock_logger: Mock logger fixture.

    Verifies that ValueError is raised when no mod entries are found.
    """
    prism_index = Path("/fake/.index")
    with patch("src.minecraft.deploy_pack.prism.load_prism_index") as mock_load, patch("pathlib.Path.is_dir") as mock_is_dir:
        mock_is_dir.return_value = True
        mock_load.return_value = []
        with pytest.raises(ValueError, match="No mod entries found"):
            deploy_pack.load_mod_list(prism_index, Path("/fake"), "server", mock_logger)


def test_copy_sync_with_mapping_basic(mock_logger, tmp_path):
    """Tests that _copy_sync_with_mapping correctly copies files and directories based on the sync mapping for both server and client sides."""
    sync_root = tmp_path / "sync"
    sync_root.mkdir()
    (sync_root / "config").mkdir()
    (sync_root / "config" / "file.txt").write_text("config")
    (sync_root / "kubejs").mkdir()
    (sync_root / "kubejs" / "script.js").write_text("kubejs")
    (sync_root / "downloads").mkdir()
    (sync_root / "resourcepacks").mkdir()
    (sync_root / "server_only").mkdir()
    (sync_root / "client_only").mkdir()
    (sync_root / "unmapped").mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    exclude_patterns = []
    sync_mapping = {
        "config": "config",
        "kubejs": "kubejs",
        "resourcepacks": {"server": "www/resourcepacks", "client": "resourcepacks"},
        "server_only": {"server": "server_stuff", "client": -1},
        "client_only": {"client": "client_stuff", "server": -1},
    }
    deploy_pack._copy_sync_with_mapping(sync_root, staging, "server", exclude_patterns, sync_mapping, mock_logger)
    assert (staging / "config" / "file.txt").exists()
    assert (staging / "kubejs" / "script.js").exists()
    assert (staging / "www" / "resourcepacks").exists()
    assert not (staging / "resourcepacks").exists()
    assert (staging / "server_stuff").exists()
    assert not (staging / "client_stuff").exists()
    assert not (staging / "unmapped").exists()
    assert not (staging / "downloads").exists()
    staging_client = tmp_path / "staging_client"
    staging_client.mkdir()
    deploy_pack._copy_sync_with_mapping(sync_root, staging_client, "client", exclude_patterns, sync_mapping, mock_logger)
    assert (staging_client / "config" / "file.txt").exists()
    assert (staging_client / "kubejs" / "script.js").exists()
    assert (staging_client / "resourcepacks").exists()
    assert not (staging_client / "www" / "resourcepacks").exists()
    assert not (staging_client / "server_stuff").exists()
    assert (staging_client / "client_stuff").exists()


def test_copy_sync_with_mapping_exclusions(mock_logger, tmp_path):
    """Tests that _copy_sync_with_mapping respects exclude_patterns and does not copy excluded directories."""
    sync_root = tmp_path / "sync"
    sync_root.mkdir()
    (sync_root / "config").mkdir()
    (sync_root / "config" / "file.txt").write_text("config")
    (sync_root / "excluded").mkdir()
    (sync_root / "excluded" / "secret.txt").write_text("secret")
    staging = tmp_path / "staging"
    staging.mkdir()
    exclude_patterns = ["excluded", "excluded/*"]
    sync_mapping = {"config": "config", "excluded": {"server": "excluded", "client": "excluded"}}
    deploy_pack._copy_sync_with_mapping(sync_root, staging, "server", exclude_patterns, sync_mapping, mock_logger)
    assert (staging / "config" / "file.txt").exists()
    assert not (staging / "excluded").exists()


def test_copy_sync_with_mapping_invalid_values(mock_logger, tmp_path):
    """Tests that _copy_sync_with_mapping logs a warning and skips entries with invalid mapping values (non-string or non--1)."""
    sync_root = tmp_path / "sync"
    sync_root.mkdir()
    (sync_root / "bad").mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    sync_mapping = {"bad": {"server": 123, "client": 456}}
    deploy_pack._copy_sync_with_mapping(sync_root, staging, "server", [], sync_mapping, mock_logger)
    mock_logger.warning.assert_called_once_with("Invalid mapping for 'bad', side 'server': expected string or -1, got <class 'int'>. Skipping.")
    assert not (staging / "bad").exists()


def test_prepare_staging(mock_logger, tmp_path):
    """Tests that prepare_staging copies mods, invokes _copy_sync_with_mapping, and returns the staging directory path."""
    modpack_dir = tmp_path / "downloads"
    modpack_dir.mkdir()
    (modpack_dir / "mod1.jar").write_text("mod1")
    (modpack_dir / "mod2.jar").write_text("mod2")
    sync_root = tmp_path / "sync"
    sync_root.mkdir()
    (sync_root / "config").mkdir()
    (sync_root / "config" / "server.properties").write_text("server")
    side_mods = [{"file": "mod1.jar", "download_url": None, "hash_value": None}, {"file": "mod2.jar", "download_url": None, "hash_value": None}]
    exclude_patterns = []
    sync_mapping = {"config": "config"}
    with (
        patch("src.minecraft.deploy_pack.file_utils.ensure_mod_file") as mock_ensure,
        patch("src.minecraft.deploy_pack._copy_sync_with_mapping") as mock_copy_sync,
        patch("src.minecraft.deploy_pack.tempfile.mkdtemp") as mock_mkdtemp,
    ):
        mock_ensure.return_value = True
        mock_mkdtemp.return_value = str(tmp_path / "staging")
        staging = deploy_pack.prepare_staging(side_mods, modpack_dir, sync_root, "server", mock_logger, exclude_patterns, sync_mapping)
        assert staging == tmp_path / "staging"
        assert (staging / "mods" / "mod1.jar").exists()
        assert (staging / "mods" / "mod2.jar").exists()
        mock_copy_sync.assert_called_once_with(sync_root, staging, "server", exclude_patterns, sync_mapping, mock_logger)
        mock_ensure.assert_any_call(modpack_dir / "mod1.jar", None, None, "sha512", mock_logger)


def test_prepare_staging_mod_download_failure(mock_logger, tmp_path):
    """Tests that prepare_staging skips a mod when ensure_mod_file returns False and logs a warning."""
    modpack_dir = tmp_path / "downloads"
    modpack_dir.mkdir()
    sync_root = tmp_path / "sync"
    sync_root.mkdir()
    side_mods = [{"file": "missing.jar", "download_url": "http://example.com", "hash_value": "abc", "hash_format": "sha512"}]
    with patch("src.minecraft.deploy_pack.file_utils.ensure_mod_file") as mock_ensure, patch("src.minecraft.deploy_pack.tempfile.mkdtemp") as mock_mkdtemp:
        mock_ensure.return_value = False
        mock_mkdtemp.return_value = str(tmp_path / "staging")
        staging = deploy_pack.prepare_staging(side_mods, modpack_dir, sync_root, "server", mock_logger, [], {})
        assert not (staging / "mods" / "missing.jar").exists()
        mock_logger.warning.assert_called_with("Skipping mod missing.jar due to missing/corrupt file")


def test_create_client_zip(mock_logger, tmp_path):
    """Tests that create_client_zip constructs the correct filename and calls create_zip_from_staging with the expected arguments."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "config").mkdir()
    www_dir = tmp_path / "www"
    filename_template = "test_{date}.zip"
    exclude_patterns = ["*.tmp"]
    with patch("src.minecraft.deploy_pack.file_utils.create_zip_from_staging") as mock_create_zip:
        result = deploy_pack.create_client_zip(staging, www_dir, filename_template, exclude_patterns, mock_logger)
        expected_name = f"test_{datetime.datetime.now(datetime.UTC).strftime('%Y%m%d')}.zip"
        expected_path = www_dir / expected_name
        assert result == expected_path
        mock_create_zip.assert_called_once_with(staging, expected_path, exclude_patterns, mock_logger)


def test_deploy_to_server(mock_logger):
    """Tests that deploy_to_server calls copy_with_exclusions with the correct staging and live server paths, exclude patterns, logger, and the clean flag set to True."""
    staging = Path("/fake/staging")
    live_server = Path("/fake/server")
    exclude_patterns = ["*.tmp"]
    with patch("src.minecraft.deploy_pack.file_utils.copy_with_exclusions") as mock_copy:
        deploy_pack.deploy_to_server(staging, live_server, exclude_patterns, mock_logger)
        mock_copy.assert_called_once_with(staging, live_server, exclude_patterns, mock_logger, clean=True)


def test_deploy_to_client(mock_logger):
    """Tests that deploy_to_client constructs the correct target .minecraft subdirectory under the MultiMC instance and passes it to copy_with_exclusions with the appropriate exclude patterns and logger."""
    staging = Path("/fake/staging")
    multimc_base = Path("/fake/multimc")
    instance_name = "TestInstance"
    exclude_patterns = ["*.tmp"]
    with patch("src.minecraft.deploy_pack.file_utils.copy_with_exclusions") as mock_copy:
        deploy_pack.deploy_to_client(staging, multimc_base, instance_name, exclude_patterns, mock_logger)
        target_dir = multimc_base / instance_name / ".minecraft"
        mock_copy.assert_called_once_with(staging, target_dir, exclude_patterns, mock_logger)


def test_main_server_default(patch_dependencies):
    """Default server mode: create zip and deploy."""
    mocks = patch_dependencies
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "sync_root": "/fake/sync",
        "live_server": "/fake/server",
        "www_dir": "/fake/www",
        "exclude_file": "/fake/sync/.rsync_exclude",
        "output_filename": "test_{date}.zip",
        "modpack_dir": "/fake/sync/downloads",
        "sync_mapping": {},
        "multimc_base": "/fake/multimc",
        "instance_name": "TestInstance",
        "logging": {"level": "INFO"},
    }.get(key, default)
    config.as_dict.return_value = {}
    mocks["mock_load_config"].return_value = config
    with patch("pathlib.Path.exists") as mock_exists:
        mock_exists.return_value = True
        with patch("sys.argv", ["deploy_pack.py"]):
            deploy_pack.main()
    mocks["mock_load_mod_list"].assert_called_once()
    mocks["mock_prepare_staging"].assert_called_once()
    mocks["mock_create_zip"].assert_called_once()
    mocks["mock_deploy_server"].assert_called_once()
    mocks["mock_deploy_client"].assert_not_called()
    mocks["mock_rmtree"].assert_called_once()


def test_main_server_no_deploy(patch_dependencies):
    """Server mode with --no-deploy: only zip."""
    mocks = patch_dependencies
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "sync_root": "/fake/sync",
        "live_server": "/fake/server",
        "www_dir": "/fake/www",
        "exclude_file": "/fake/sync/.rsync_exclude",
        "output_filename": "test_{date}.zip",
        "modpack_dir": "/fake/sync/downloads",
        "sync_mapping": {},
        "multimc_base": "/fake/multimc",
        "instance_name": "TestInstance",
        "logging": {"level": "INFO"},
    }.get(key, default)
    config.as_dict.return_value = {}
    mocks["mock_load_config"].return_value = config
    with patch("pathlib.Path.exists") as mock_exists:
        mock_exists.return_value = True
        with patch("sys.argv", ["deploy_pack.py", "--no-deploy"]):
            deploy_pack.main()
    mocks["mock_create_zip"].assert_called_once()
    mocks["mock_deploy_server"].assert_not_called()


def test_main_server_no_zip(patch_dependencies):
    """Server mode with --no-zip: only deploy."""
    mocks = patch_dependencies
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "sync_root": "/fake/sync",
        "live_server": "/fake/server",
        "www_dir": "/fake/www",
        "exclude_file": "/fake/sync/.rsync_exclude",
        "output_filename": "test_{date}.zip",
        "modpack_dir": "/fake/sync/downloads",
        "sync_mapping": {},
        "multimc_base": "/fake/multimc",
        "instance_name": "TestInstance",
        "logging": {"level": "INFO"},
    }.get(key, default)
    config.as_dict.return_value = {}
    mocks["mock_load_config"].return_value = config
    with patch("pathlib.Path.exists") as mock_exists:
        mock_exists.return_value = True
        with patch("sys.argv", ["deploy_pack.py", "--no-zip"]):
            deploy_pack.main()
    mocks["mock_create_zip"].assert_not_called()
    mocks["mock_deploy_server"].assert_called_once()


def test_main_server_no_zip_no_deploy(patch_dependencies):
    """Both flags -> warning and no actions."""
    mocks = patch_dependencies
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "sync_root": "/fake/sync",
        "live_server": "/fake/server",
        "www_dir": "/fake/www",
        "exclude_file": "/fake/sync/.rsync_exclude",
        "output_filename": "test_{date}.zip",
        "modpack_dir": "/fake/sync/downloads",
        "sync_mapping": {},
        "multimc_base": "/fake/multimc",
        "instance_name": "TestInstance",
        "logging": {"level": "INFO"},
    }.get(key, default)
    config.as_dict.return_value = {}
    mocks["mock_load_config"].return_value = config
    with patch("pathlib.Path.exists") as mock_exists:
        mock_exists.return_value = True
        with patch("sys.argv", ["deploy_pack.py", "--no-zip", "--no-deploy"]):
            deploy_pack.main()
    mocks["mock_logger"].warning.assert_called_with("Both --no-zip and --no-deploy specified - nothing will be done.")
    mocks["mock_create_zip"].assert_not_called()
    mocks["mock_deploy_server"].assert_not_called()


def test_main_client_mode(patch_dependencies):
    """Client mode with instance_name set."""
    mocks = patch_dependencies
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "sync_root": "/fake/sync",
        "live_server": "/fake/server",
        "www_dir": "/fake/www",
        "exclude_file": "/fake/sync/.rsync_exclude",
        "output_filename": "test_{date}.zip",
        "modpack_dir": "/fake/sync/downloads",
        "sync_mapping": {},
        "multimc_base": "/fake/multimc",
        "instance_name": "TestInstance",
        "logging": {"level": "INFO"},
    }.get(key, default)
    config.as_dict.return_value = {}
    mocks["mock_load_config"].return_value = config
    with patch("pathlib.Path.exists") as mock_exists:
        mock_exists.return_value = True
        with patch("sys.argv", ["deploy_pack.py", "--client"]):
            deploy_pack.main()
    mocks["mock_load_mod_list"].assert_called_once()
    mocks["mock_prepare_staging"].assert_called_once()
    mocks["mock_create_zip"].assert_not_called()
    mocks["mock_deploy_server"].assert_not_called()
    mocks["mock_deploy_client"].assert_called_once_with(Path("/fake/staging"), Path("/fake/multimc"), "TestInstance", ["*.tmp", "*.bak"], mocks["mock_logger"])


def test_main_client_mode_missing_instance_name(patch_dependencies):
    """Client mode should exit early if instance_name is missing."""
    mocks = patch_dependencies
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "sync_root": "/fake/sync",
        "live_server": "/fake/server",
        "www_dir": "/fake/www",
        "exclude_file": "/fake/sync/.rsync_exclude",
        "output_filename": "test_{date}.zip",
        "modpack_dir": "/fake/sync/downloads",
        "sync_mapping": {},
        "multimc_base": "/fake/multimc",
        "logging": {"level": "INFO"},
    }.get(key, default)
    config.as_dict.return_value = {}
    mocks["mock_load_config"].return_value = config
    with patch("sys.argv", ["deploy_pack.py", "--client"]):
        with pytest.raises(SystemExit) as exc:
            deploy_pack.main()
        assert exc.value.code == 1
        mocks["mock_logger"].error.assert_called_once_with("instance_name must be set for client mode")
        mocks["mock_load_mod_list"].assert_not_called()
        mocks["mock_prepare_staging"].assert_not_called()


def test_main_config_dir_file(patch_dependencies):
    """If --config-dir points to a file, use its parent."""
    mocks = patch_dependencies
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "sync_root": "/fake/sync",
        "live_server": "/fake/server",
        "www_dir": "/fake/www",
        "exclude_file": "/fake/sync/.rsync_exclude",
        "output_filename": "test_{date}.zip",
        "modpack_dir": "/fake/sync/downloads",
        "sync_mapping": {},
        "multimc_base": "/fake/multimc",
        "instance_name": "TestInstance",
        "logging": {"level": "INFO"},
    }.get(key, default)
    config.as_dict.return_value = {}
    mocks["mock_load_config"].return_value = config
    with patch("pathlib.Path.is_file") as mock_is_file:
        mock_is_file.return_value = True
        with patch("pathlib.Path.exists") as mock_exists:
            mock_exists.return_value = True
            with patch("sys.argv", ["deploy_pack.py", "--config-dir", "/some/path/file.toml"]):
                deploy_pack.main()
    mocks["mock_load_config"].assert_called_once()
    _args, kwargs = mocks["mock_load_config"].call_args
    assert kwargs["config_dir"] == Path("/some/path")


def test_main_debug(patch_dependencies):
    """Debug flag should set logging level and print config."""
    mocks = patch_dependencies
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "sync_root": "/fake/sync",
        "live_server": "/fake/server",
        "www_dir": "/fake/www",
        "exclude_file": "/fake/sync/.rsync_exclude",
        "output_filename": "test_{date}.zip",
        "modpack_dir": "/fake/sync/downloads",
        "sync_mapping": {},
        "multimc_base": "/fake/multimc",
        "instance_name": "TestInstance",
        "logging": {"level": "INFO"},
    }.get(key, default)
    config.as_dict.return_value = {"foo": "bar"}
    mocks["mock_load_config"].return_value = config
    with patch("pathlib.Path.exists") as mock_exists:
        mock_exists.return_value = True
        with patch("sys.argv", ["deploy_pack.py", "--debug"]), patch("builtins.print") as mock_print:
            deploy_pack.main()
            mock_print.assert_any_call("=== Loaded configuration ===")
            mock_print.assert_any_call("foo = bar")


def test_main_load_mods_failure(patch_dependencies):
    """If load_mod_list fails, exit with error."""
    mocks = patch_dependencies
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "sync_root": "/fake/sync",
        "live_server": "/fake/server",
        "www_dir": "/fake/www",
        "exclude_file": "/fake/sync/.rsync_exclude",
        "output_filename": "test_{date}.zip",
        "modpack_dir": "/fake/sync/downloads",
        "sync_mapping": {},
        "multimc_base": "/fake/multimc",
        "instance_name": "TestInstance",
        "logging": {"level": "INFO"},
    }.get(key, default)
    config.as_dict.return_value = {}
    mocks["mock_load_config"].return_value = config
    mocks["mock_load_mod_list"].side_effect = ValueError("Bad index")
    with patch("sys.argv", ["deploy_pack.py"]):
        with pytest.raises(SystemExit) as exc:
            deploy_pack.main()
        assert exc.value.code == 1
        mocks["mock_logger"].error.assert_called_with("Failed to load mods: Bad index")


def test_main_prepare_staging_failure(patch_dependencies):
    """If prepare_staging raises, exit with error."""
    mocks = patch_dependencies
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "sync_root": "/fake/sync",
        "live_server": "/fake/server",
        "www_dir": "/fake/www",
        "exclude_file": "/fake/sync/.rsync_exclude",
        "output_filename": "test_{date}.zip",
        "modpack_dir": "/fake/sync/downloads",
        "sync_mapping": {},
        "multimc_base": "/fake/multimc",
        "instance_name": "TestInstance",
        "logging": {"level": "INFO"},
    }.get(key, default)
    config.as_dict.return_value = {}
    mocks["mock_load_config"].return_value = config
    mocks["mock_prepare_staging"].side_effect = OSError("Disk full")
    with patch("sys.argv", ["deploy_pack.py"]):
        with pytest.raises(SystemExit) as exc:
            deploy_pack.main()
        assert exc.value.code == 1
        mocks["mock_logger"].exception.assert_called_with("Deployment failed")
