# tests/unit/deploy_pack/test_deploy_pack.py

"""Unit tests for deploy_pack.py."""

import datetime
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.minecraft import deploy_pack

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_logger():
    """Return a MagicMock logger."""
    return MagicMock()


@pytest.fixture
def sync_tree(tmp_path):
    """Build a representative sync/ tree."""
    sync_root = tmp_path / "sync"
    sync_root.mkdir()
    (sync_root / "config").mkdir()
    (sync_root / "config" / "file.txt").write_text("config")
    (sync_root / "kubejs").mkdir()
    (sync_root / "kubejs" / "script.js").write_text("kubejs")
    (sync_root / "downloads").mkdir()  # always skipped
    (sync_root / "resourcepacks").mkdir()
    (sync_root / "resourcepacks" / "pack.zip").write_text("rp")
    (sync_root / "server_only").mkdir()
    (sync_root / "client_only").mkdir()
    (sync_root / "unmapped").mkdir()
    return sync_root


# ---------------------------------------------------------------------------
# load_mod_list
# ---------------------------------------------------------------------------


def test_load_mod_list_success(mock_logger):
    """load_mod_list should load, override, filter, and log."""
    prism_index = Path("/fake/.index")
    config_dir = Path("/fake/config")
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
        result = deploy_pack.load_mod_list(prism_index, config_dir, "server", mock_logger)
        assert len(result) == 1
        mock_load.assert_called_once_with(prism_index)
        mock_load_overrides.assert_called_once_with(config_dir / "side_overrides.toml")
        mock_apply.assert_called_once()
        mock_filter.assert_called_once_with(mock_apply.return_value, "server")
        mock_logger.info.assert_any_call("Loaded 1 mods from Prism index")
        mock_logger.info.assert_any_call("Filtered to 1 mods for side 'server'")


def test_load_mod_list_index_missing(mock_logger):
    """Should raise ValueError if index dir missing."""
    with patch("pathlib.Path.is_dir") as mock_is_dir:
        mock_is_dir.return_value = False
        with pytest.raises(ValueError, match="Prism index directory not found"):
            deploy_pack.load_mod_list(Path("/fake/.index"), Path("/fake"), "server", mock_logger)


def test_load_mod_list_empty(mock_logger):
    """Should raise ValueError if index is empty."""
    with (
        patch("src.minecraft.deploy_pack.prism.load_prism_index") as mock_load,
        patch("pathlib.Path.is_dir") as mock_is_dir,
    ):
        mock_is_dir.return_value = True
        mock_load.return_value = []
        with pytest.raises(ValueError, match="No mod entries found"):
            deploy_pack.load_mod_list(Path("/fake/.index"), Path("/fake"), "server", mock_logger)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def test_resolve_mapping_for_side_string():
    """String mapping applies to both sides."""
    assert deploy_pack.resolve_mapping_for_side("config", "server") == "config"
    assert deploy_pack.resolve_mapping_for_side("config", "client") == "config"


def test_resolve_mapping_for_side_dict():
    """Dict mapping picks the side-specific value."""
    m = {"server": "srv", "client": "cli"}
    assert deploy_pack.resolve_mapping_for_side(m, "server") == "srv"
    assert deploy_pack.resolve_mapping_for_side(m, "client") == "cli"


def test_resolve_mapping_for_side_excluded():
    """-1 / missing side key means excluded (None)."""
    m = {"server": "srv", "client": -1}
    assert deploy_pack.resolve_mapping_for_side(m, "server") == "srv"
    assert deploy_pack.resolve_mapping_for_side(m, "client") is None
    assert deploy_pack.resolve_mapping_for_side({"server": "x"}, "client") is None


def test_resolve_mapping_for_side_invalid():
    """Non-string, non--1 values yield None."""
    assert deploy_pack.resolve_mapping_for_side({"server": 123}, "server") is None
    assert deploy_pack.resolve_mapping_for_side(42, "server") is None


def test_is_shared_dest():
    """Only @-prefixed destinations are shared."""
    assert deploy_pack.is_shared_dest("@www/resourcepacks") is True
    assert deploy_pack.is_shared_dest("@mods/foo") is True
    assert deploy_pack.is_shared_dest("config") is False
    assert deploy_pack.is_shared_dest("www/resourcepacks") is False


def test_resolve_shared_dest(tmp_path):
    """@www/* and @mods/* resolve under www_dir / mods_dir."""
    www = tmp_path / "www"
    mods = tmp_path / "mods"
    assert deploy_pack.resolve_shared_dest("@www", www, mods) == www
    assert deploy_pack.resolve_shared_dest("@www/rp", www, mods) == www / "rp"
    assert deploy_pack.resolve_shared_dest("@mods", www, mods) == mods
    assert deploy_pack.resolve_shared_dest("@mods/x", www, mods) == mods / "x"


def test_resolve_shared_dest_unknown(tmp_path):
    """Unknown @-prefix should raise ValueError."""
    with pytest.raises(ValueError, match="Unknown shared destination"):
        deploy_pack.resolve_shared_dest("@bogus/x", tmp_path, tmp_path)


# ---------------------------------------------------------------------------
# Staging builders
# ---------------------------------------------------------------------------


def test_prepare_mods_staging(mock_logger, tmp_path):
    """prepare_mods_staging should copy verified mod files under mods/."""
    modpack_dir = tmp_path / "downloads"
    modpack_dir.mkdir()
    (modpack_dir / "mod1.jar").write_text("mod1")
    side_mods = [{"file": "mod1.jar", "download_url": None, "hash_value": None}]
    with patch("src.minecraft.deploy_pack.file_utils.ensure_mod_file") as mock_ensure:
        mock_ensure.return_value = True
        staging = deploy_pack.prepare_mods_staging(side_mods, modpack_dir, mock_logger)
        try:
            assert (staging / "mods" / "mod1.jar").exists()
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        mock_ensure.assert_called_once_with(modpack_dir / "mod1.jar", None, None, "sha512", mock_logger)


def test_prepare_mods_staging_download_failure(mock_logger, tmp_path):
    """Failed downloads should be skipped with a warning."""
    modpack_dir = tmp_path / "downloads"
    modpack_dir.mkdir()
    side_mods = [{"file": "missing.jar", "download_url": "http://x", "hash_value": "abc"}]
    with patch("src.minecraft.deploy_pack.file_utils.ensure_mod_file") as mock_ensure:
        mock_ensure.return_value = False
        staging = deploy_pack.prepare_mods_staging(side_mods, modpack_dir, mock_logger)
        try:
            assert not (staging / "mods" / "missing.jar").exists()
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        mock_logger.warning.assert_any_call("Skipping mod missing.jar due to missing/corrupt file")


def test_prepare_instance_staging(mock_logger, sync_tree):
    """Instance staging should include non-shared, side-appropriate items only."""
    sync_mapping = {
        "config": "config",
        "kubejs": "kubejs",
        "resourcepacks": {"server": "@www/resourcepacks", "client": "resourcepacks"},
        "server_only": {"server": "server_stuff", "client": -1},
        "client_only": {"client": "client_stuff", "server": -1},
    }
    staging = deploy_pack.prepare_instance_staging(sync_tree, "server", [], sync_mapping, mock_logger)
    try:
        assert (staging / "config" / "file.txt").exists()
        assert (staging / "kubejs" / "script.js").exists()
        assert (staging / "server_stuff").exists()
        assert not (staging / "client_stuff").exists()
        # @-prefixed items must not appear in instance staging
        assert not (staging / "www").exists()
        assert not (staging / "resourcepacks").exists()
        # unmapped items skipped
        assert not (staging / "unmapped").exists()
        # downloads always skipped
        assert not (staging / "downloads").exists()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def test_prepare_instance_staging_client(mock_logger, sync_tree):
    """Client side of instance staging picks client-only destinations."""
    sync_mapping = {
        "config": "config",
        "resourcepacks": {"server": "@www/resourcepacks", "client": "resourcepacks"},
        "client_only": {"client": "client_stuff", "server": -1},
    }
    staging = deploy_pack.prepare_instance_staging(sync_tree, "client", [], sync_mapping, mock_logger)
    try:
        assert (staging / "config" / "file.txt").exists()
        assert (staging / "client_stuff").exists()
        assert not (staging / "www").exists()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def test_prepare_client_staging(mock_logger, tmp_path):
    """Client staging should contain client mods plus client-mapped sync items."""
    sync_root = tmp_path / "sync"
    sync_root.mkdir()
    (sync_root / "config").mkdir()
    (sync_root / "config" / "file.txt").write_text("config")
    (sync_root / "resourcepacks").mkdir()
    (sync_root / "resourcepacks" / "rp.zip").write_text("rp")

    modpack_dir = tmp_path / "downloads"
    modpack_dir.mkdir()
    (modpack_dir / "client_mod.jar").write_text("m")

    client_mods = [{"file": "client_mod.jar", "download_url": None, "hash_value": None}]
    sync_mapping = {
        "config": "config",
        "resourcepacks": {"server": "@www/resourcepacks", "client": "resourcepacks"},
    }
    with patch("src.minecraft.deploy_pack.file_utils.ensure_mod_file") as mock_ensure:
        mock_ensure.return_value = True
        staging = deploy_pack.prepare_client_staging(client_mods, modpack_dir, sync_root, [], sync_mapping, mock_logger)
    try:
        assert (staging / "mods" / "client_mod.jar").exists()
        assert (staging / "config" / "file.txt").exists()
        assert (staging / "resourcepacks" / "rp.zip").exists()
        # @-destinations must NOT enter the client ZIP
        assert not (staging / "www").exists()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# deploy_shared_items
# ---------------------------------------------------------------------------


def test_deploy_shared_items(mock_logger, sync_tree, tmp_path):
    """Shared items go to www_dir / mods_dir; non-shared items are ignored."""
    www_dir = tmp_path / "www"
    mods_dir = tmp_path / "mods"
    sync_mapping = {
        "resourcepacks": {"server": "@www/resourcepacks", "client": "resourcepacks"},
        "config": "config",
    }
    deploy_pack.deploy_shared_items(sync_tree, "server", [], sync_mapping, www_dir, mods_dir, mock_logger)
    assert (www_dir / "resourcepacks" / "pack.zip").exists()
    # `config` is instance-mapped, not shared
    assert not (www_dir / "config").exists()


# ---------------------------------------------------------------------------
# create_client_zip / deploy_to_server / deploy_to_client
# ---------------------------------------------------------------------------


def test_create_client_zip(mock_logger, tmp_path):
    """create_client_zip should template the filename with the current date."""
    staging = tmp_path / "staging"
    staging.mkdir()
    www_dir = tmp_path / "www"
    with patch("src.minecraft.deploy_pack.file_utils.create_zip_from_staging") as mock_zip:
        result = deploy_pack.create_client_zip(staging, www_dir, "test_{date}.zip", ["*.tmp"], mock_logger)
        expected = www_dir / f"test_{datetime.datetime.now(datetime.UTC).strftime('%Y%m%d')}.zip"
        assert result == expected
        mock_zip.assert_called_once_with(staging, expected, ["*.tmp"], mock_logger)


def test_deploy_to_server(mock_logger):
    """deploy_to_server should call copy_with_exclusions with clean=True."""
    with patch("src.minecraft.deploy_pack.file_utils.copy_with_exclusions") as mock_copy:
        deploy_pack.deploy_to_server(Path("/s"), Path("/srv"), ["*.tmp"], mock_logger)
        mock_copy.assert_called_once_with(Path("/s"), Path("/srv"), ["*.tmp"], mock_logger, clean=True)


def test_deploy_to_client(mock_logger):
    """deploy_to_client should target <multimc_base>/<instance>/.minecraft."""
    with patch("src.minecraft.deploy_pack.file_utils.copy_with_exclusions") as mock_copy:
        deploy_pack.deploy_to_client(Path("/s"), Path("/mmc"), "Inst", ["*.tmp"], mock_logger)
        mock_copy.assert_called_once_with(Path("/s"), Path("/mmc") / "Inst" / ".minecraft", ["*.tmp"], mock_logger)


# ---------------------------------------------------------------------------
# load_instances
# ---------------------------------------------------------------------------


def test_load_instances_dict_form(mock_logger):
    """[instances.x] path = ... form parses cleanly."""
    config = MagicMock()
    config.get.return_value = {
        "survival": {"path": "/srv/survival"},
        "creative": {"path": "/srv/creative"},
    }
    result = deploy_pack.load_instances(config, mock_logger)
    assert ("survival", Path("/srv/survival")) in result
    assert ("creative", Path("/srv/creative")) in result


def test_load_instances_flat_form(mock_logger):
    """Instances = { name = path } form parses cleanly."""
    config = MagicMock()
    config.get.return_value = {"survival": "./survival", "creative": "./creative"}
    result = deploy_pack.load_instances(config, mock_logger)
    assert ("survival", Path("./survival")) in result
    assert ("creative", Path("./creative")) in result


def test_load_instances_empty(mock_logger):
    """Empty instances dict yields an empty list."""
    config = MagicMock()
    config.get.return_value = {}
    assert deploy_pack.load_instances(config, mock_logger) == []


def test_load_instances_missing_path(mock_logger):
    """Instance with empty path is skipped with a warning."""
    config = MagicMock()
    config.get.return_value = {"survival": {"path": ""}}
    result = deploy_pack.load_instances(config, mock_logger)
    assert result == []
    mock_logger.warning.assert_called()


# ---------------------------------------------------------------------------
# main() -- shared fixtures
# ---------------------------------------------------------------------------


def _make_main_config(instances=None):
    """Return a MagicMock Config for main() tests."""
    if instances is None:
        instances = {
            "survival": {"path": "/fake/survival"},
            "creative": {"path": "/fake/creative"},
        }
    values = {
        "sync_root": "/fake/sync",
        "mods_dir": "/fake/mods",
        "www_dir": "/fake/www",
        "exclude_file": "/fake/sync/.rsync_exclude",
        "output_filename": "test_{date}.zip",
        "modpack_dir": "/fake/sync/downloads",
        "sync_mapping": {},
        "instances": instances,
        "multimc_base": "/fake/multimc",
        "instance_name": "TestInstance",
        "logging": {"level": "INFO"},
    }
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.as_dict.return_value = {}
    return config


@pytest.fixture
def patch_dependencies():
    """Patch every deploy_pack call used by main()."""
    with (
        patch("src.minecraft.deploy_pack.cfg.load_config") as mock_load_config,
        patch("src.minecraft.deploy_pack.setup_logging"),
        patch("src.minecraft.deploy_pack.get_logger") as mock_get_logger,
        patch("src.minecraft.deploy_pack.file_utils.get_exclude_patterns") as mock_get_exclude,
        patch("src.minecraft.deploy_pack.load_mod_list") as mock_load_mod_list,
        patch("src.minecraft.deploy_pack.prepare_mods_staging") as mock_prepare_mods,
        patch("src.minecraft.deploy_pack.prepare_instance_staging") as mock_prepare_instance,
        patch("src.minecraft.deploy_pack.prepare_client_staging") as mock_prepare_client,
        patch("src.minecraft.deploy_pack.deploy_shared_items") as mock_deploy_shared,
        patch("src.minecraft.deploy_pack.create_client_zip") as mock_create_zip,
        patch("src.minecraft.deploy_pack.deploy_to_server") as mock_deploy_server,
        patch("src.minecraft.deploy_pack.deploy_to_client") as mock_deploy_client,
        patch("src.minecraft.deploy_pack.file_utils.copy_with_exclusions") as mock_copy_mods,
        patch("src.minecraft.deploy_pack.shutil.rmtree") as mock_rmtree,
    ):
        logger = MagicMock()
        mock_get_logger.return_value = logger
        mock_get_exclude.return_value = ["*.tmp"]
        mock_load_mod_list.return_value = [{"file": "mod1.jar"}]
        mock_prepare_mods.return_value = Path("/fake/mods_staging")
        mock_prepare_instance.return_value = Path("/fake/instance_staging")
        mock_prepare_client.return_value = Path("/fake/client_staging")
        yield {
            "mock_load_config": mock_load_config,
            "mock_logger": logger,
            "mock_get_exclude": mock_get_exclude,
            "mock_load_mod_list": mock_load_mod_list,
            "mock_prepare_mods": mock_prepare_mods,
            "mock_prepare_instance": mock_prepare_instance,
            "mock_prepare_client": mock_prepare_client,
            "mock_deploy_shared": mock_deploy_shared,
            "mock_create_zip": mock_create_zip,
            "mock_deploy_server": mock_deploy_server,
            "mock_deploy_client": mock_deploy_client,
            "mock_copy_mods": mock_copy_mods,
            "mock_rmtree": mock_rmtree,
        }


# ---------------------------------------------------------------------------
# main() -- server mode
# ---------------------------------------------------------------------------


def test_main_server_default(patch_dependencies):
    """Default server mode: mods -> instances -> shared -> ZIP."""
    mocks = patch_dependencies
    mocks["mock_load_config"].return_value = _make_main_config()
    with patch("sys.argv", ["deploy_pack.py"]):
        deploy_pack.main()

    # load_mod_list called once for server, once for client ZIP
    assert mocks["mock_load_mod_list"].call_count == 2
    mocks["mock_prepare_mods"].assert_called_once()
    # one instance staging per instance
    assert mocks["mock_prepare_instance"].call_count == 2
    mocks["mock_deploy_server"].assert_called()
    mocks["mock_deploy_shared"].assert_called_once()
    mocks["mock_prepare_client"].assert_called_once()
    mocks["mock_create_zip"].assert_called_once()
    mocks["mock_deploy_client"].assert_not_called()


def test_main_server_no_deploy(patch_dependencies):
    """--no-deploy: skip mods/instances/shared, still build ZIP."""
    mocks = patch_dependencies
    mocks["mock_load_config"].return_value = _make_main_config()
    with patch("sys.argv", ["deploy_pack.py", "--no-deploy"]):
        deploy_pack.main()

    mocks["mock_deploy_server"].assert_not_called()
    mocks["mock_deploy_shared"].assert_not_called()
    mocks["mock_prepare_mods"].assert_not_called()
    mocks["mock_prepare_instance"].assert_not_called()
    mocks["mock_create_zip"].assert_called_once()


def test_main_server_no_zip(patch_dependencies):
    """--no-zip: deploy but do not create client ZIP."""
    mocks = patch_dependencies
    mocks["mock_load_config"].return_value = _make_main_config()
    with patch("sys.argv", ["deploy_pack.py", "--no-zip"]):
        deploy_pack.main()

    mocks["mock_create_zip"].assert_not_called()
    mocks["mock_prepare_client"].assert_not_called()
    mocks["mock_deploy_server"].assert_called()


def test_main_server_no_zip_no_deploy(patch_dependencies):
    """Both flags -> warning, no work."""
    mocks = patch_dependencies
    mocks["mock_load_config"].return_value = _make_main_config()
    with patch("sys.argv", ["deploy_pack.py", "--no-zip", "--no-deploy"]):
        deploy_pack.main()

    mocks["mock_create_zip"].assert_not_called()
    mocks["mock_deploy_server"].assert_not_called()
    mocks["mock_logger"].warning.assert_any_call("Both --no-zip and --no-deploy specified - nothing will be done.")


def test_main_server_no_instances(patch_dependencies):
    """No instances configured + deploy requested -> exit 1."""
    mocks = patch_dependencies
    mocks["mock_load_config"].return_value = _make_main_config(instances={})
    with patch("sys.argv", ["deploy_pack.py"]):
        with pytest.raises(SystemExit) as exc:
            deploy_pack.main()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# main() -- client mode
# ---------------------------------------------------------------------------


def test_main_client_mode(patch_dependencies):
    """Client mode: build client staging and deploy to MultiMC."""
    mocks = patch_dependencies
    mocks["mock_load_config"].return_value = _make_main_config()
    with patch("sys.argv", ["deploy_pack.py", "--client"]):
        deploy_pack.main()

    mocks["mock_load_mod_list"].assert_called_once()
    mocks["mock_prepare_client"].assert_called_once()
    mocks["mock_deploy_client"].assert_called_once_with(
        Path("/fake/client_staging"),
        Path("/fake/multimc"),
        "TestInstance",
        ["*.tmp"],
        mocks["mock_logger"],
    )
    mocks["mock_deploy_server"].assert_not_called()
    mocks["mock_create_zip"].assert_not_called()


def test_main_client_mode_missing_instance_name(patch_dependencies):
    """Client mode without instance_name should exit early."""
    mocks = patch_dependencies
    cfg = _make_main_config()
    original = cfg.get.side_effect

    def g(k, d=None):
        if k == "instance_name":
            return None
        return original(k, d)

    cfg.get.side_effect = g
    mocks["mock_load_config"].return_value = cfg
    with patch("sys.argv", ["deploy_pack.py", "--client"]):
        with pytest.raises(SystemExit) as exc:
            deploy_pack.main()
        assert exc.value.code == 1
        mocks["mock_logger"].error.assert_any_call("instance_name must be set for client mode")


# ---------------------------------------------------------------------------
# main() -- misc
# ---------------------------------------------------------------------------


def test_main_config_dir_file(patch_dependencies):
    """--config-dir pointing at a file uses its parent dir."""
    mocks = patch_dependencies
    mocks["mock_load_config"].return_value = _make_main_config()
    with (
        patch("pathlib.Path.is_file", return_value=True),
        patch("sys.argv", ["deploy_pack.py", "--config-dir", "/some/path/file.toml"]),
    ):
        deploy_pack.main()
    _args, kwargs = mocks["mock_load_config"].call_args
    assert kwargs["config_dir"] == Path("/some/path")


def test_main_debug(patch_dependencies):
    """--debug prints the config dict."""
    mocks = patch_dependencies
    cfg = _make_main_config()
    cfg.as_dict.return_value = {"foo": "bar"}
    mocks["mock_load_config"].return_value = cfg
    with (
        patch("sys.argv", ["deploy_pack.py", "--debug"]),
        patch("builtins.print") as mock_print,
    ):
        deploy_pack.main()
        mock_print.assert_any_call("=== Loaded configuration ===")
        mock_print.assert_any_call("foo = bar")


def test_main_load_mods_failure(patch_dependencies):
    """If load_mod_list raises, exit 1 and log the exception."""
    mocks = patch_dependencies
    mocks["mock_load_config"].return_value = _make_main_config()
    mocks["mock_load_mod_list"].side_effect = ValueError("Bad index")
    with patch("sys.argv", ["deploy_pack.py"]):
        with pytest.raises(SystemExit) as exc:
            deploy_pack.main()
        assert exc.value.code == 1
        mocks["mock_logger"].exception.assert_called()


def test_main_prepare_staging_failure(patch_dependencies):
    """If a staging step raises, exit 1 and log the exception."""
    mocks = patch_dependencies
    mocks["mock_load_config"].return_value = _make_main_config()
    mocks["mock_prepare_mods"].side_effect = OSError("Disk full")
    with patch("sys.argv", ["deploy_pack.py"]):
        with pytest.raises(SystemExit) as exc:
            deploy_pack.main()
        assert exc.value.code == 1
        mocks["mock_logger"].exception.assert_called()
