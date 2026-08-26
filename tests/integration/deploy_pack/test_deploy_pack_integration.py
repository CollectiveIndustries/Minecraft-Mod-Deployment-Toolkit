"""
Integration tests for deploy_pack.py – runs main() with real file I/O.
"""

import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from minecraft import deploy_pack

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def write_config_file(config_dir, content):
    """Write a TOML config file."""
    config_file = config_dir / "deploy_pack.toml"
    config_file.write_text(content)
    return config_file


def create_dummy_files(base_dir, structure):
    """Create dummy files/dirs from a dict: {'path/to/file': 'content'}."""
    for rel_path, content in structure.items():
        full = base_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)


# ----------------------------------------------------------------------
# Fixture
# ----------------------------------------------------------------------


@pytest.fixture
def temp_dirs():
    """Create temporary directories for integration tests."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        sync = base / "sync"
        live = base / "server"
        www = base / "www"
        config_dir = base / "config.d"
        for d in [sync, live, www, config_dir]:
            d.mkdir()
        yield base, sync, live, www, config_dir


# ----------------------------------------------------------------------
# Integration tests
# ----------------------------------------------------------------------


@patch("minecraft.deploy_pack.setup_logging")
@patch("minecraft.deploy_pack.get_logger")
def test_integration_server_mode(mock_get_logger, mock_setup_logging, temp_dirs):
    """
    Integration test for server mode: creates a ZIP from real staging.
    """
    base, sync, live, www, config_dir = temp_dirs

    # Write a real config file (use single quotes to avoid escaping issues)
    config_content = f"""
sync_root = '{sync}'
live_server = '{live}'
www_dir = '{www}'
exclude_file = '{config_dir / ".rsync_exclude"}'
output_filename = "minecraft_client_{{date}}.zip"
multimc_base = "/fake"
instance_name = "test"
"""
    write_config_file(config_dir, config_content)

    # Populate source directories according to script expectations:
    # - sync_root/client/ contains mod JARs directly
    # - sync_root/config/ contains config files
    # - live_server/kubejs/ and live_server/config/ftbquests/
    create_dummy_files(
        sync / "client",
        {
            "fake_mod.jar": "dummy mod content",
            "backup.bak": "should be excluded",
        },
    )
    create_dummy_files(
        sync / "config",
        {
            "server.properties": "dummy server properties",
            "some_other_config.cfg": "dummy config",
            "options.txt": "client options",  # now from config, not from client
        },
    )
    create_dummy_files(
        live,
        {
            "kubejs/startup_scripts/script.js": "// dummy",
            "config/ftbquests/quests.json": "{}",
        },
    )
    # Create exclude file with patterns
    (config_dir / ".rsync_exclude").write_text("*.bak\n*.tmp\n")

    # Mock logger
    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    # Run main() with --server and explicit --config-dir
    with patch(
        "sys.argv", ["deploy_pack.py", "--server", "--config-dir", str(config_dir)]
    ):
        deploy_pack.main()

    # Verify the ZIP was created
    zip_files = list(www.glob("minecraft_client_*.zip"))
    assert len(zip_files) == 1
    zip_path = zip_files[0]

    # Inspect the zip contents – expected layout:
    #   mods/fake_mod.jar
    #   config/server.properties
    #   config/some_other_config.cfg
    #   config/options.txt
    #   kubejs/startup_scripts/script.js
    #   config/ftbquests/quests.json
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "mods/fake_mod.jar" in names
        assert "mods/backup.bak" not in names  # excluded
        assert "config/server.properties" in names
        assert "config/some_other_config.cfg" in names
        assert "config/options.txt" in names
        assert "kubejs/startup_scripts/script.js" in names
        assert "config/ftbquests/quests.json" in names

    mock_logger.info.assert_any_call("Mode: server")
    mock_logger.info.assert_any_call("Client pack created successfully.")


@patch("minecraft.deploy_pack.setup_logging")
@patch("minecraft.deploy_pack.get_logger")
def test_integration_client_mode(mock_get_logger, mock_setup_logging, temp_dirs):
    """
    Integration test for client mode: deploys to a MultiMC instance folder.
    """
    base, sync, live, www, config_dir = temp_dirs

    # Fake MultiMC instance directory
    multimc_base = base / "multimc" / "instances"
    instance_name = "TestInstance"
    target_dir = multimc_base / instance_name / ".minecraft"

    # Write config with MultiMC settings
    config_content = f"""
sync_root = '{sync}'
live_server = '{live}'
www_dir = '{www}'
exclude_file = '{config_dir / ".rsync_exclude"}'
output_filename = "minecraft_client_{{date}}.zip"
multimc_base = '{multimc_base}'
instance_name = '{instance_name}'
"""
    write_config_file(config_dir, config_content)

    # Populate source directories
    create_dummy_files(
        sync / "client",
        {
            "fake_mod.jar": "dummy mod",
            "backup.bak": "should be excluded",
        },
    )
    create_dummy_files(
        sync / "config",
        {
            "server.properties": "dummy server properties",
            "some_other_config.cfg": "dummy config",
            "options.txt": "client options",
        },
    )
    create_dummy_files(
        live,
        {
            "kubejs/startup_scripts/script.js": "// code",
            "config/ftbquests/quests.json": "{}",
        },
    )
    # Exclude file
    (config_dir / ".rsync_exclude").write_text("*.bak\n*.tmp\n")

    # Mock logger
    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    # Run main() with --client and --config-dir
    with patch(
        "sys.argv", ["deploy_pack.py", "--client", "--config-dir", str(config_dir)]
    ):
        deploy_pack.main()

    # Verify files were copied to target .minecraft in the correct locations
    assert (target_dir / "mods" / "fake_mod.jar").exists()
    assert not (target_dir / "mods" / "backup.bak").exists()  # excluded
    assert (target_dir / "config" / "server.properties").exists()
    assert (target_dir / "config" / "some_other_config.cfg").exists()
    assert (target_dir / "config" / "options.txt").exists()
    assert (target_dir / "kubejs" / "startup_scripts" / "script.js").exists()
    assert (target_dir / "config" / "ftbquests" / "quests.json").exists()

    mock_logger.info.assert_any_call("Mode: client")
    mock_logger.info.assert_any_call("Client deployment completed successfully.")
