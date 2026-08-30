"""Integration tests for deploy_pack.py."""

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from src.minecraft import deploy_pack


@pytest.fixture
def temp_dirs(tmp_path):
    """Create temporary directory structure for integration tests."""
    base = tmp_path / "deploy_test"
    sync = base / "sync"
    live = base / "server"
    www = base / "www"
    config_dir = base / "config.d"
    modpack = base / "modpack"
    # Prism index is always inside modpack/.index
    prism_index = modpack / ".index"
    for d in [sync, live, www, config_dir, modpack, prism_index]:
        d.mkdir(parents=True, exist_ok=True)
    return base, sync, live, www, config_dir, modpack


def write_config_file(config_dir: Path, content: str):
    config_file = config_dir / "deploy_pack.toml"
    config_file.write_text(content)


def create_dummy_files(base_dir: Path, files: dict):
    for rel_path, content in files.items():
        path = base_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def create_prism_index(index_dir: Path, mods: list):
    """Create .pw.toml files for each mod in the list, with actual hashes."""
    for mod in mods:
        filename = mod["file"]
        name = mod.get("name", filename)
        side = mod.get("side", "both")
        if "content" in mod:
            hash_value = hashlib.sha512(mod["content"].encode()).hexdigest()
        else:
            hash_value = "dummyhash"
        content = f"""
filename = "{filename}"
name = "{name}"
side = "{side}"

[download]
url = "http://example.com/dummy.jar"
hash = "{hash_value}"
hash-format = "sha512"

[update.curseforge]
project-id = 123
file-id = 456
"""
        (index_dir / f"{filename}.pw.toml").write_text(content)


def test_integration_server_mode(temp_dirs):
    """
    Integration test for server mode: creates a ZIP from real staging.
    Uses Prism index located in modpack/.index.
    """
    _base, sync, live, www, config_dir, modpack = temp_dirs

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

    # Create Prism index inside modpack/.index
    prism_index = modpack / ".index"
    mods = [{"file": "fake_mod.jar", "side": "both", "content": "dummy mod content"}]
    create_prism_index(prism_index, mods)

    # Place mod files in modpack directory
    create_dummy_files(
        modpack,
        {
            "fake_mod.jar": "dummy mod content",
            "backup.bak": "should be excluded",
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
    (config_dir / ".rsync_exclude").write_text("*.bak\n*.tmp\n")

    with patch(
        "sys.argv",
        [
            "deploy_pack.py",
            "--server",
            "--config-dir",
            str(config_dir),
        ],
    ):
        deploy_pack.main()

    zip_files = list(www.glob("minecraft_client_*.zip"))
    assert len(zip_files) == 1
    assert zip_files[0].is_file()


def test_integration_client_mode(temp_dirs):
    """
    Integration test for client mode: deploys to a MultiMC instance folder.
    Uses Prism index located in modpack/.index.
    """
    _base, sync, live, www, config_dir, modpack = temp_dirs

    multimc_base = _base / "multimc" / "instances"
    instance_name = "TestInstance"
    target_dir = multimc_base / instance_name / ".minecraft"

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

    # Create Prism index inside modpack/.index
    prism_index = modpack / ".index"
    mods = [{"file": "fake_mod.jar", "side": "both", "content": "dummy mod"}]
    create_prism_index(prism_index, mods)

    # Place mod files in modpack
    create_dummy_files(
        modpack,
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
    (config_dir / ".rsync_exclude").write_text("*.bak\n*.tmp\n")

    with patch(
        "sys.argv",
        [
            "deploy_pack.py",
            "--client",
            "--config-dir",
            str(config_dir),
        ],
    ):
        deploy_pack.main()

    assert (target_dir / "mods" / "fake_mod.jar").exists()
    assert (target_dir / "config" / "server.properties").exists()
    assert (target_dir / "kubejs" / "startup_scripts" / "script.js").exists()
    assert (target_dir / "config" / "ftbquests" / "quests.json").exists()
    assert not (target_dir / "mods" / "backup.bak").exists()
