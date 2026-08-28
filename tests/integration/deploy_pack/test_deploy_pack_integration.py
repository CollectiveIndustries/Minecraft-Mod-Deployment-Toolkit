"""
Integration tests for deploy_pack.py - runs main() with real file I/O.
"""

import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

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
        modpack = base / "modpack"
        for d in [sync, live, www, config_dir, modpack]:
            d.mkdir()
        yield base, sync, live, www, config_dir, modpack


# ----------------------------------------------------------------------
# Integration tests
# ----------------------------------------------------------------------


def test_integration_server_mode(temp_dirs):
    """
    Integration test for server mode: creates a ZIP from real staging.
    Now uses a manifest and modpack directory.
    """
    _base, sync, live, www, config_dir, modpack = temp_dirs

    # Write a real config file
    config_content = f"""
sync_root = '{sync}'
live_server = '{live}'
www_dir = '{www}'
exclude_file = '{config_dir / ".rsync_exclude"}'
output_filename = "minecraft_client_{{date}}.zip"
modpack_dir = '{modpack}'
multimc_base = "/fake"
instance_name = "test"
"""
    write_config_file(config_dir, config_content)

    # Create the manifest (config.d/manifest.yaml)
    manifest = {
        "mods": [
            {
                "id": "fake_mod",
                "file": "fake_mod.jar",
                "side": "both",
                "enabled": True,
            }
        ]
    }
    import yaml

    with (config_dir / "manifest.yaml").open("w") as f:
        yaml.dump(manifest, f)

    # Place mod files in modpack directory
    create_dummy_files(
        modpack,
        {
            "fake_mod.jar": "dummy mod content",
            "backup.bak": "should be excluded",  # not referenced in manifest, will be ignored
        },
    )

    # Populate source directories for config, kubejs, etc.
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
            "kubejs/startup_scripts/script.js": "// dummy",
            "config/ftbquests/quests.json": "{}",
        },
    )
    # Create exclude file with patterns
    (config_dir / ".rsync_exclude").write_text("*.bak\n*.tmp\n")

    # Run main() with --server and explicit --config-dir
    with patch(
        "sys.argv", ["deploy_pack.py", "--server", "--config-dir", str(config_dir)]
    ):
        deploy_pack.main()

    # Verify the ZIP was created
    zip_files = list(www.glob("minecraft_client_*.zip"))
    assert len(zip_files) == 1
    zip_path = zip_files[0]

    # Inspect the zip contents - expected layout:
    #   mods/fake_mod.jar
    #   config/server.properties
    #   config/some_other_config.cfg
    #   config/options.txt
    #   kubejs/startup_scripts/script.js
    #   config/ftbquests/quests.json
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "mods/fake_mod.jar" in names
        assert "mods/backup.bak" not in names  # not in manifest
        assert "config/server.properties" in names
        assert "config/some_other_config.cfg" in names
        assert "config/options.txt" in names
        assert "kubejs/startup_scripts/script.js" in names
        assert "config/ftbquests/quests.json" in names


def test_integration_client_mode(temp_dirs):
    """
    Integration test for client mode: deploys to a MultiMC instance folder.
    Uses manifest to decide which mods to copy.
    """
    base, sync, live, www, config_dir, modpack = temp_dirs

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
modpack_dir = '{modpack}'
multimc_base = '{multimc_base}'
instance_name = '{instance_name}'
"""
    write_config_file(config_dir, config_content)

    # Create manifest
    manifest = {
        "mods": [
            {
                "id": "fake_mod",
                "file": "fake_mod.jar",
                "side": "both",
                "enabled": True,
            }
        ]
    }
    import yaml

    with (config_dir / "manifest.yaml").open("w") as f:
        yaml.dump(manifest, f)

    # Place mod files in modpack
    create_dummy_files(
        modpack,
        {
            "fake_mod.jar": "dummy mod",
            "backup.bak": "should be excluded",  # not referenced
        },
    )

    # Populate source directories
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

    # Run main() with --client and --config-dir
    with patch(
        "sys.argv", ["deploy_pack.py", "--client", "--config-dir", str(config_dir)]
    ):
        deploy_pack.main()

    # Verify files were copied to target .minecraft in the correct locations
    assert (target_dir / "mods" / "fake_mod.jar").exists()
    assert not (target_dir / "mods" / "backup.bak").exists()
    assert (target_dir / "config" / "server.properties").exists()
    assert (target_dir / "config" / "some_other_config.cfg").exists()
    assert (target_dir / "config" / "options.txt").exists()
    assert (target_dir / "kubejs" / "startup_scripts" / "script.js").exists()
    assert (target_dir / "config" / "ftbquests" / "quests.json").exists()
