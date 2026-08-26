"""
Unit tests for mod_puller.py.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The module under test
from minecraft import mod_puller

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def mock_logger():
    """Mock logger returned by get_logger."""
    with patch("minecraft.mod_puller.get_logger") as mock_get:
        logger = MagicMock()
        mock_get.return_value = logger
        yield logger


@pytest.fixture
def mock_config():
    """Mock ConfigManager and config."""
    with patch("minecraft.mod_puller.ConfigManager") as MockCM:
        mock_mgr = MagicMock()
        mock_mgr.file.return_value = mock_mgr
        mock_mgr.env.return_value = mock_mgr
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "CF_API_KEY": "test_key",
            "output_root": "./modpack",
            "manifest_file": "manifest.yaml",
            "minecraft_version": "1.20.1",
        }.get(key, default)
        mock_mgr.load.return_value = mock_config
        MockCM.return_value = mock_mgr
        yield mock_mgr, mock_config


# ----------------------------------------------------------------------
# Tests for find_config_file
# ----------------------------------------------------------------------


def test_find_config_file(tmp_path):
    """find_config_file should return first existing file."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    toml = config_dir / "mod_puller.toml"
    toml.touch()
    yaml_file = config_dir / "mod_puller.yaml"
    yaml_file.touch()

    result = mod_puller.find_config_file(config_dir)
    assert result == toml

    toml.unlink()
    result = mod_puller.find_config_file(config_dir)
    assert result == yaml_file

    yaml_file.unlink()
    result = mod_puller.find_config_file(config_dir)
    assert result is None


# ----------------------------------------------------------------------
# Tests for load_manifest
# ----------------------------------------------------------------------


def test_load_manifest(tmp_path):
    """load_manifest should parse YAML and return mods list."""
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text("mods:\n  - id: test\n    source: local\n")
    mods = mod_puller.load_manifest(manifest_path)
    assert mods == [{"id": "test", "source": "local"}]


def test_load_manifest_missing():
    """load_manifest should raise FileNotFoundError if missing."""
    with pytest.raises(FileNotFoundError):
        mod_puller.load_manifest(Path("/nonexistent"))


def test_load_manifest_empty():
    """load_manifest should return empty list if no mods key."""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("other: data\n")
        path = Path(f.name)
    mods = mod_puller.load_manifest(path)
    assert mods == []
    path.unlink()


# ----------------------------------------------------------------------
# Tests for get_mod_file_url (mocking CurseForgeAPy)
# ----------------------------------------------------------------------


@patch("minecraft.mod_puller.CurseForgeAPI")
def test_get_mod_file_url(mock_api):
    """get_mod_file_url should find matching file and return URL."""
    # Setup mock API responses
    mock_client = mock_api.return_value
    mock_search = MagicMock()
    mock_search.data = [MagicMock(id=123)]
    mock_client.searchMods.return_value = mock_search

    mock_file = MagicMock()
    mock_file.id = 456
    mock_file.displayName = "1.0.0"
    mock_file.fileName = "mod-1.0.0.jar"
    mock_file.gameVersions = [MagicMock(gameId=432, versionString="1.20.1")]
    mock_client.getModFiles.return_value.data = [mock_file]

    mock_dl = MagicMock()
    mock_dl.data.downloadUrl = "https://example.com/mod.jar"
    mock_client.getModFileDownloadUrl.return_value = mock_dl

    url, filename = mod_puller.get_mod_file_url(
        mock_client, "testmod", "1.0.0", "1.20.1"
    )
    assert url == "https://example.com/mod.jar"
    assert filename == "mod-1.0.0.jar"


def test_get_mod_file_url_not_found():
    """get_mod_file_url should raise if no matching mod."""
    with patch("minecraft.mod_puller.CurseForgeAPI") as mock_api:
        mock_client = mock_api.return_value
        mock_search = MagicMock()
        mock_search.data = []  # no results
        mock_client.searchMods.return_value = mock_search

        with pytest.raises(ValueError, match="Mod 'unknown' not found"):
            mod_puller.get_mod_file_url(mock_client, "unknown", "1.0", "1.20.1")


def test_get_mod_file_url_no_version_match():
    """get_mod_file_url should raise if no matching version."""
    with patch("minecraft.mod_puller.CurseForgeAPI") as mock_api:
        mock_client = mock_api.return_value
        mock_search = MagicMock()
        mock_search.data = [MagicMock(id=123)]
        mock_client.searchMods.return_value = mock_search

        mock_file = MagicMock()
        mock_file.id = 456
        mock_file.displayName = "2.0.0"
        mock_file.fileName = "mod-2.0.0.jar"
        mock_file.gameVersions = [MagicMock(gameId=432, versionString="1.20.1")]
        mock_client.getModFiles.return_value.data = [mock_file]

        with pytest.raises(
            ValueError, match="No file found for 'testmod' version '1.0.0' on MC 1.20.1"
        ):
            mod_puller.get_mod_file_url(mock_client, "testmod", "1.0.0", "1.20.1")


# ----------------------------------------------------------------------
# Tests for download_file
# ----------------------------------------------------------------------


@patch("minecraft.mod_puller.requests.get")
def test_download_file(mock_get, tmp_path):
    """download_file should write response content to file."""
    mock_response = MagicMock()
    mock_response.iter_content.return_value = [b"chunk1", b"chunk2"]
    mock_get.return_value = mock_response

    output = tmp_path / "mod.jar"
    mod_puller.download_file("http://example.com", output)
    assert output.exists()
    assert output.read_bytes() == b"chunk1chunk2"


@patch("minecraft.mod_puller.requests.get")
def test_download_file_creates_dirs(mock_get, tmp_path):
    """download_file should create parent directories."""
    mock_response = MagicMock()
    mock_response.iter_content.return_value = [b"data"]
    mock_get.return_value = mock_response

    output = tmp_path / "sub" / "dir" / "mod.jar"
    mod_puller.download_file("http://example.com", output)
    assert output.exists()


# ----------------------------------------------------------------------
# Tests for main() (with heavy mocking)
# ----------------------------------------------------------------------


@patch("minecraft.mod_puller.find_config_file")
@patch("minecraft.mod_puller.ConfigManager")
@patch("minecraft.mod_puller.setup_logging")
@patch("minecraft.mod_puller.get_logger")
@patch("minecraft.mod_puller.load_manifest")
@patch("minecraft.mod_puller.CurseForgeAPI")
@patch("minecraft.mod_puller.download_file")
def test_main_curseforge_mod(
    mock_download,
    mock_api,
    mock_load_manifest,
    mock_get_logger,
    mock_setup_logging,
    mock_ConfigManager,
    mock_find_config,
    tmp_path,
):
    """main() should download curseforge mods and place them correctly."""
    # Prepare mocks
    mock_find_config.return_value = tmp_path / "config.d" / "mod_puller.toml"
    mock_mgr = MagicMock()
    mock_mgr.file.return_value = mock_mgr
    mock_mgr.env.return_value = mock_mgr
    mock_config = MagicMock()
    mock_config.get.side_effect = lambda key, default=None: {
        "CF_API_KEY": "test_key",
        "output_root": str(tmp_path / "modpack"),
        "manifest_file": "manifest.yaml",
        "minecraft_version": "1.20.1",
    }.get(key, default)
    mock_mgr.load.return_value = mock_config
    mock_ConfigManager.return_value = mock_mgr

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    # Manifest with one curseforge mod
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

    # Mock CurseForge API to return a download URL
    mock_client = mock_api.return_value
    mock_get_url = MagicMock(
        return_value=("http://example.com/create.jar", "create.jar")
    )
    with patch("minecraft.mod_puller.get_mod_file_url", mock_get_url):
        # Run main
        with patch("sys.argv", ["mod_puller.py"]):
            mod_puller.main()

    # Assert download called with correct path
    expected_path = tmp_path / "modpack" / "mods" / "create.jar"
    mock_download.assert_called_once_with(
        "http://example.com/create.jar", expected_path
    )
    mock_logger.info.assert_any_call("Mod puller finished.")


@patch("minecraft.mod_puller.find_config_file")
@patch("minecraft.mod_puller.ConfigManager")
@patch("minecraft.mod_puller.setup_logging")
@patch("minecraft.mod_puller.get_logger")
@patch("minecraft.mod_puller.load_manifest")
def test_main_local_file(
    mock_load_manifest,
    mock_get_logger,
    mock_setup_logging,
    mock_ConfigManager,
    mock_find_config,
    tmp_path,
):
    """main() should handle local files correctly."""
    mock_find_config.return_value = tmp_path / "config.d" / "mod_puller.toml"
    mock_mgr = MagicMock()
    mock_mgr.file.return_value = mock_mgr
    mock_mgr.env.return_value = mock_mgr
    mock_config = MagicMock()
    mock_config.get.side_effect = lambda key, default=None: {
        "CF_API_KEY": "test_key",
        "output_root": str(tmp_path / "modpack"),
        "manifest_file": "manifest.yaml",
        "minecraft_version": "1.20.1",
    }.get(key, default)
    mock_mgr.load.return_value = mock_config
    mock_ConfigManager.return_value = mock_mgr

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

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

    with patch("sys.argv", ["mod_puller.py"]):
        mod_puller.main()

    # The actual log uses an f-string: f"Local file exists: {target_path}"
    mock_logger.info.assert_any_call(f"Local file exists: {local_jar}")
    mock_logger.info.assert_any_call("Mod puller finished.")


@patch("minecraft.mod_puller.find_config_file")
@patch("minecraft.mod_puller.ConfigManager")
@patch("minecraft.mod_puller.setup_logging")
@patch("minecraft.mod_puller.get_logger")
@patch("minecraft.mod_puller.load_manifest")
def test_main_disabled_mod(
    mock_load_manifest,
    mock_get_logger,
    mock_setup_logging,
    mock_ConfigManager,
    mock_find_config,
    tmp_path,
):
    """main() should skip disabled mods."""
    mock_find_config.return_value = tmp_path / "config.d" / "mod_puller.toml"
    mock_mgr = MagicMock()
    mock_mgr.file.return_value = mock_mgr
    mock_mgr.env.return_value = mock_mgr
    mock_config = MagicMock()
    mock_config.get.side_effect = lambda key, default=None: {
        "CF_API_KEY": "test_key",
        "output_root": str(tmp_path / "modpack"),
        "manifest_file": "manifest.yaml",
        "minecraft_version": "1.20.1",
    }.get(key, default)
    mock_mgr.load.return_value = mock_config
    mock_ConfigManager.return_value = mock_mgr

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

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

    with patch("sys.argv", ["mod_puller.py"]):
        mod_puller.main()

    mock_logger.info.assert_any_call("Skipping disabled mod: disabled")
    mock_logger.info.assert_any_call("Mod puller finished.")


@patch("minecraft.mod_puller.find_config_file")
@patch("minecraft.mod_puller.ConfigManager")
@patch("minecraft.mod_puller.setup_logging")
@patch("minecraft.mod_puller.get_logger")
@patch("minecraft.mod_puller.load_manifest")
def test_main_no_api_key(
    mock_load_manifest,
    mock_get_logger,
    mock_setup_logging,
    mock_ConfigManager,
    mock_find_config,
):
    """main() should exit if API key missing."""
    mock_find_config.return_value = None
    mock_mgr = MagicMock()
    mock_mgr.file.return_value = mock_mgr
    mock_mgr.env.return_value = mock_mgr
    mock_config = MagicMock()
    mock_config.get.return_value = None  # No API key
    mock_mgr.load.return_value = mock_config
    mock_ConfigManager.return_value = mock_mgr

    with patch("sys.argv", ["mod_puller.py"]), pytest.raises(SystemExit) as exc:
        mod_puller.main()
    assert exc.value.code == 1
    # The print to stderr happens before logging is set up
    # We can't easily capture it, but we can check that the function exited.
