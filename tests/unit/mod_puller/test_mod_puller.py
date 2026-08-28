"""
Unit tests for mod_puller.py - adapted to new common modules.
"""

from unittest.mock import MagicMock, patch

import pytest

from minecraft import mod_puller


@pytest.fixture
def mock_logger():
    with patch("minecraft.mod_puller.get_logger") as mock_get:
        logger = MagicMock()
        mock_get.return_value = logger
        yield logger


@patch("minecraft.mod_puller.cfg.load_combined_config")
@patch("minecraft.mod_puller.manifest_utils.load_manifest")
@patch("minecraft.mod_puller.file_utils.download_file")
@patch("minecraft.mod_puller.cf.get_download_url_by_ids")
@patch("minecraft.mod_puller.cf.get_mod_file_url_by_slug")
@patch("minecraft.mod_puller.setup_logging")
@patch("minecraft.mod_puller.get_logger")
def test_main_curseforge_mod_direct(
    mock_get_logger,
    mock_setup_logging,
    mock_get_slug,
    mock_get_url_by_ids,
    mock_download,
    mock_load_manifest,
    mock_load_config,
    tmp_path,
):
    """Test main with a curseforge mod using project_id/file_id."""
    # Config
    config_obj = MagicMock()
    config_obj.get.side_effect = lambda key, default=None: {
        "CF_API_KEY": "test_key",
        "output_root": str(tmp_path / "modpack"),
        "manifest_file": "manifest.yaml",
        "minecraft_version": "1.20.1",
    }.get(key, default)
    mock_load_config.return_value = config_obj

    # Manifest with direct IDs
    mock_load_manifest.return_value = [
        {
            "id": "create",
            "source": "curseforge",
            "project_id": 123,
            "file_id": 456,
            "file": "mods/create.jar",
            "enabled": True,
        }
    ]

    mock_get_url_by_ids.return_value = "http://example.com/create.jar"

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    with patch("sys.argv", ["mod_puller.py"]):
        mod_puller.main()

    expected_path = tmp_path / "modpack" / "mods" / "create.jar"
    mock_download.assert_called_once_with(
        "http://example.com/create.jar", expected_path
    )
    mock_get_url_by_ids.assert_called_once_with(123, 456, "test_key")
    mock_get_slug.assert_not_called()
    mock_logger.info.assert_any_call("Mod puller finished.")


@patch("minecraft.mod_puller.cfg.load_combined_config")
@patch("minecraft.mod_puller.manifest_utils.load_manifest")
@patch("minecraft.mod_puller.file_utils.download_file")
@patch("minecraft.mod_puller.cf.get_mod_file_url_by_slug")
@patch("minecraft.mod_puller.setup_logging")
@patch("minecraft.mod_puller.get_logger")
def test_main_curseforge_mod_fallback(
    mock_get_logger,
    mock_setup_logging,
    mock_get_slug,
    mock_download,
    mock_load_manifest,
    mock_load_config,
    tmp_path,
):
    """Test fallback to slug/version when IDs are missing."""
    config_obj = MagicMock()
    config_obj.get.side_effect = lambda key, default=None: {
        "CF_API_KEY": "test_key",
        "output_root": str(tmp_path / "modpack"),
        "manifest_file": "manifest.yaml",
        "minecraft_version": "1.20.1",
    }.get(key, default)
    mock_load_config.return_value = config_obj

    mock_load_manifest.return_value = [
        {
            "id": "create",
            "source": "curseforge",
            "slug": "create",
            "version": "6.0.8",
            "file": "mods/create.jar",
            "enabled": True,
        }
    ]

    mock_get_slug.return_value = ("http://example.com/create.jar", "create.jar")

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    with patch("sys.argv", ["mod_puller.py"]):
        mod_puller.main()

    expected_path = tmp_path / "modpack" / "mods" / "create.jar"
    mock_download.assert_called_once_with(
        "http://example.com/create.jar", expected_path
    )
    mock_get_slug.assert_called_once_with("create", "6.0.8", "1.20.1", "test_key")
    mock_logger.info.assert_any_call("Mod puller finished.")


@patch("minecraft.mod_puller.cfg.load_combined_config")
@patch("minecraft.mod_puller.manifest_utils.load_manifest")
@patch("minecraft.mod_puller.setup_logging")
@patch("minecraft.mod_puller.get_logger")
def test_main_local_file(
    mock_get_logger,
    mock_setup_logging,
    mock_load_manifest,
    mock_load_config,
    tmp_path,
):
    """Test local file handling."""
    config_obj = MagicMock()
    config_obj.get.side_effect = lambda key, default=None: {
        "CF_API_KEY": "test_key",
        "output_root": str(tmp_path / "modpack"),
        "manifest_file": "manifest.yaml",
        "minecraft_version": "1.20.1",
    }.get(key, default)
    mock_load_config.return_value = config_obj

    # Create a local file that exists
    output_root = tmp_path / "modpack"
    output_root.mkdir()
    mods_dir = output_root / "mods"
    mods_dir.mkdir()
    local_jar = mods_dir / "local.jar"
    local_jar.touch()

    mock_load_manifest.return_value = [
        {
            "id": "localmod",
            "source": "local",
            "file": "mods/local.jar",
            "enabled": True,
        }
    ]

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    with patch("sys.argv", ["mod_puller.py"]):
        mod_puller.main()

    mock_logger.info.assert_any_call(f"Local file exists: {local_jar}")
    mock_logger.info.assert_any_call("Mod puller finished.")


@patch("minecraft.mod_puller.cfg.load_combined_config")
@patch("minecraft.mod_puller.manifest_utils.load_manifest")
@patch("minecraft.mod_puller.setup_logging")
@patch("minecraft.mod_puller.get_logger")
def test_main_disabled_mod(
    mock_get_logger,
    mock_setup_logging,
    mock_load_manifest,
    mock_load_config,
    tmp_path,
):
    """Test that disabled mods are skipped."""
    config_obj = MagicMock()
    config_obj.get.side_effect = lambda key, default=None: {
        "CF_API_KEY": "test_key",
        "output_root": str(tmp_path / "modpack"),
        "manifest_file": "manifest.yaml",
        "minecraft_version": "1.20.1",
    }.get(key, default)
    mock_load_config.return_value = config_obj

    mock_load_manifest.return_value = [
        {
            "id": "disabled",
            "source": "curseforge",
            "slug": "disabled",
            "version": "1.0",
            "file": "mods/disabled.jar",
            "enabled": False,
        }
    ]

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    with patch("sys.argv", ["mod_puller.py"]):
        mod_puller.main()

    mock_logger.info.assert_any_call("Skipping disabled mod: disabled")
    mock_logger.info.assert_any_call("Mod puller finished.")


@patch("minecraft.mod_puller.cfg.load_combined_config")
@patch("minecraft.mod_puller.setup_logging")
@patch("minecraft.mod_puller.get_logger")
def test_main_no_api_key(
    mock_get_logger,
    mock_setup_logging,
    mock_load_config,
):
    """Should exit if API key is missing."""
    config_obj = MagicMock()
    config_obj.get.return_value = None  # No API key
    mock_load_config.return_value = config_obj

    with patch("sys.argv", ["mod_puller.py"]), pytest.raises(SystemExit) as exc:
        mod_puller.main()
    assert exc.value.code == 1
