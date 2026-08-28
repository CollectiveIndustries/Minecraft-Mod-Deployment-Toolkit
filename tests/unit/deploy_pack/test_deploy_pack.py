"""
Unit tests for deploy_pack.py - testing main() with mocks.
Helper functions are now in common modules, so we only test the main integration.
"""

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from minecraft import deploy_pack


@pytest.fixture
def temp_dirs():
    """Create temporary directories for testing."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        sync = base / "sync"
        live = base / "server"
        www = base / "www"
        config_dir = base / "config.d"
        for d in [sync, live, www, config_dir]:
            d.mkdir()
        yield base, sync, live, www, config_dir


@patch("minecraft.deploy_pack.cfg.load_combined_config")
@patch("minecraft.deploy_pack.manifest_utils.load_manifest")
@patch("minecraft.deploy_pack.file_utils.get_exclude_patterns")
@patch("minecraft.deploy_pack.file_utils.copy_directory_contents")
@patch("minecraft.deploy_pack.file_utils.create_zip_from_staging")
@patch("minecraft.deploy_pack.tempfile.TemporaryDirectory")
@patch("minecraft.deploy_pack.setup_logging")
@patch("minecraft.deploy_pack.get_logger")
def test_main_server_mode(
    mock_get_logger,
    mock_setup_logging,
    mock_tempdir,
    mock_create_zip,
    mock_copy_dir,
    mock_get_exclude,
    mock_load_manifest,
    mock_load_config,
    temp_dirs,
):
    """Test server mode main flow."""
    base, sync, live, www, config_dir = temp_dirs

    # Build mock config using actual temporary paths
    mock_config = {
        "sync_root": str(sync),
        "live_server": str(live),
        "www_dir": str(www),
        "exclude_file": str(config_dir / ".rsync_exclude"),
        "output_filename": "minecraft_client_{date}.zip",
        "multimc_base": str(Path.home() / ".local/share/multimc/instances"),
        "instance_name": "Mike_N_Ike",
        "modpack_dir": str(sync / "downloads"),
    }
    config_obj = MagicMock()
    config_obj.get.side_effect = lambda key, default=None: mock_config.get(key, default)
    mock_load_config.return_value = config_obj

    mock_load_manifest.return_value = [
        {"id": "testmod", "file": "mods/test.jar", "side": "both"}
    ]

    mock_get_exclude.return_value = []
    mock_tempdir.return_value.__enter__.return_value = str(base / "staging")

    with patch.object(Path, "is_dir", return_value=True):
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger

        with patch("sys.argv", ["deploy_pack.py"]):
            deploy_pack.main()

    mock_load_config.assert_called_once()
    mock_load_manifest.assert_called_once()
    mock_setup_logging.assert_called_once()
    # copy_directory_contents should be called for: config, scripts, kubejs, ftbquests (4)
    assert mock_copy_dir.call_count == 4
    mock_create_zip.assert_called_once()
    mock_logger.info.assert_any_call("Mode: server")


@patch("minecraft.deploy_pack.cfg.load_combined_config")
@patch("minecraft.deploy_pack.manifest_utils.load_manifest")
@patch("minecraft.deploy_pack.file_utils.get_exclude_patterns")
@patch("minecraft.deploy_pack.file_utils.copy_directory_contents")
@patch("minecraft.deploy_pack.file_utils.copy_with_exclusions")
@patch("minecraft.deploy_pack.tempfile.TemporaryDirectory")
@patch("minecraft.deploy_pack.setup_logging")
@patch("minecraft.deploy_pack.get_logger")
def test_main_client_mode(
    mock_get_logger,
    mock_setup_logging,
    mock_tempdir,
    mock_copy_exclusions,
    mock_copy_dir,
    mock_get_exclude,
    mock_load_manifest,
    mock_load_config,
    temp_dirs,
):
    """Test client mode main flow."""
    base, sync, live, www, config_dir = temp_dirs

    mock_config = {
        "sync_root": str(sync),
        "live_server": str(live),
        "www_dir": str(www),
        "exclude_file": str(config_dir / ".rsync_exclude"),
        "output_filename": "minecraft_client_{date}.zip",
        "multimc_base": str(Path.home() / ".local/share/multimc/instances"),
        "instance_name": "Mike_N_Ike",
        "modpack_dir": str(sync / "downloads"),
    }
    config_obj = MagicMock()
    config_obj.get.side_effect = lambda key, default=None: mock_config.get(key, default)
    mock_load_config.return_value = config_obj

    mock_load_manifest.return_value = [
        {"id": "testmod", "file": "mods/test.jar", "side": "client"}
    ]
    mock_get_exclude.return_value = []
    mock_tempdir.return_value.__enter__.return_value = str(base / "staging")

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    with patch("sys.argv", ["deploy_pack.py", "--client"]):
        deploy_pack.main()

    expected_target = (
        Path.home() / ".local/share/multimc/instances/Mike_N_Ike/.minecraft"
    )
    mock_copy_exclusions.assert_called_once()
    args, _ = mock_copy_exclusions.call_args
    assert args[1] == expected_target
    mock_logger.info.assert_any_call("Mode: client")


def test_main_client_mode_missing_instance_name():
    """Client mode should exit if instance_name is missing."""
    with patch("minecraft.deploy_pack.cfg.load_combined_config") as mock_load_config:
        config_obj = MagicMock()
        config_obj.get.side_effect = lambda key, default=None: (
            {"sync_root": "/fake", "live_server": "/fake", "www_dir": "/fake"}.get(
                key, default
            )
            if key != "instance_name"
            else None
        )
        mock_load_config.return_value = config_obj

        with (
            patch("minecraft.deploy_pack.setup_logging"),
            patch("minecraft.deploy_pack.get_logger") as mock_get_logger,
            patch("minecraft.deploy_pack.tempfile.TemporaryDirectory"),
            patch("sys.argv", ["deploy_pack.py", "--client"]),
        ):
            mock_logger = MagicMock()
            mock_get_logger.return_value = mock_logger
            with pytest.raises(SystemExit) as exc:
                deploy_pack.main()
            assert exc.value.code == 1


def test_argparse_mode():
    """Simple argparse test."""
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--server", action="store_true")
    group.add_argument("--client", action="store_true")
    with patch("sys.argv", ["deploy_pack.py"]):
        args, _ = parser.parse_known_args()
        assert args.server is False
        assert args.client is False
    with patch("sys.argv", ["deploy_pack.py", "--server"]):
        args, _ = parser.parse_known_args()
        assert args.server is True
    with patch("sys.argv", ["deploy_pack.py", "--client"]):
        args, _ = parser.parse_known_args()
        assert args.client is True
