"""
Unit tests for manifest_builder.py.
"""

import sys
import zipfile
from pathlib import Path
from unittest.mock import ANY, MagicMock, mock_open, patch

import pytest

# The module under test
from minecraft import manifest_builder


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

@pytest.fixture
def mock_logger():
    """Mock logger returned by get_logger."""
    with patch('minecraft.manifest_builder.get_logger') as mock_get:
        logger = MagicMock()
        mock_get.return_value = logger
        yield logger


# ----------------------------------------------------------------------
# Tests for find_jars
# ----------------------------------------------------------------------

def test_find_jars(tmp_path, mock_logger):
    """find_jars should yield all .jar files recursively."""
    dir1 = tmp_path / "mods1"
    dir2 = tmp_path / "mods2"
    dir1.mkdir()
    dir2.mkdir()

    (dir1 / "a.jar").touch()
    (dir1 / "sub").mkdir()
    (dir1 / "sub" / "b.jar").touch()
    (dir2 / "c.jar").touch()
    (dir2 / "d.txt").touch()

    jars = list(manifest_builder.find_jars([dir1, dir2], mock_logger))
    assert len(jars) == 3
    assert any(j.name == "a.jar" for j in jars)
    assert any(j.name == "b.jar" for j in jars)
    assert any(j.name == "c.jar" for j in jars)
    mock_logger.warning.assert_not_called()


def test_find_jars_missing_dir(tmp_path, mock_logger):
    """find_jars should warn and skip missing directories."""
    missing = tmp_path / "missing"
    jars = list(manifest_builder.find_jars([missing], mock_logger))
    assert jars == []
    mock_logger.warning.assert_called_once_with(f"{missing} is not a directory, skipping")


# ----------------------------------------------------------------------
# Tests for extract_metadata
# ----------------------------------------------------------------------

def test_extract_metadata_success(tmp_path, mock_logger):
    """extract_metadata should read mods.toml and return correct data."""
    jar_path = tmp_path / "test.jar"
    # Create a fake jar with META-INF/mods.toml
    toml_content = """
[[mods]]
modId = "testmod"
version = "1.0.0"
displayName = "Test Mod"
side = "CLIENT"

[[mods.dependencies]]
modId = "dep1"
mandatory = true

[[mods.dependencies]]
modId = "dep2"
mandatory = false
"""
    with zipfile.ZipFile(jar_path, 'w') as zf:
        zf.writestr("META-INF/mods.toml", toml_content)

    result = manifest_builder.extract_metadata(jar_path, mock_logger)
    assert result == {
        "id": "testmod",
        "version": "1.0.0",
        "display_name": "Test Mod",
        "side": "client",
        "depends": ["dep1", "dep2"],
    }


def test_extract_metadata_no_toml(tmp_path, mock_logger):
    """extract_metadata should return None if no metadata file found."""
    jar_path = tmp_path / "test.jar"
    with zipfile.ZipFile(jar_path, 'w') as zf:
        zf.writestr("META-INF/other.txt", "dummy")

    result = manifest_builder.extract_metadata(jar_path, mock_logger)
    assert result is None
    mock_logger.debug.assert_called_once_with(f"No metadata file found in {jar_path}")


def test_extract_metadata_neoforge_toml(tmp_path, mock_logger):
    """extract_metadata should fall back to neoforge.mods.toml."""
    jar_path = tmp_path / "test.jar"
    toml_content = """
[[mods]]
modId = "neoforge_mod"
version = "2.0.0"
displayName = "NeoForge Mod"
side = "SERVER"
"""
    with zipfile.ZipFile(jar_path, 'w') as zf:
        zf.writestr("META-INF/neoforge.mods.toml", toml_content)

    result = manifest_builder.extract_metadata(jar_path, mock_logger)
    assert result["id"] == "neoforge_mod"
    assert result["side"] == "server"


def test_extract_metadata_corrupt_toml(tmp_path, mock_logger):
    """extract_metadata should handle corrupt TOML gracefully."""
    jar_path = tmp_path / "test.jar"
    with zipfile.ZipFile(jar_path, 'w') as zf:
        zf.writestr("META-INF/mods.toml", "this is not toml [")

    result = manifest_builder.extract_metadata(jar_path, mock_logger)
    assert result is None
    mock_logger.error.assert_called_once()


# ----------------------------------------------------------------------
# Tests for build_manifest
# ----------------------------------------------------------------------

def test_build_manifest(tmp_path, mock_logger):
    """build_manifest should build correct entries from jars."""
    root = tmp_path / "root"
    root.mkdir()
    mods_dir = root / "mods"
    mods_dir.mkdir()

    jar1 = mods_dir / "mod1.jar"
    toml1 = """
[[mods]]
modId = "mod1"
version = "1.0"
displayName = "Mod One"
side = "BOTH"
"""
    with zipfile.ZipFile(jar1, 'w') as zf:
        zf.writestr("META-INF/mods.toml", toml1)

    jar2 = mods_dir / "mod2.jar"
    toml2 = """
[[mods]]
modId = "mod2"
version = "2.0"
displayName = "Mod Two"
side = "CLIENT"
dependencies = ["depX"]
"""
    with zipfile.ZipFile(jar2, 'w') as zf:
        zf.writestr("META-INF/mods.toml", toml2)

    jars = [jar1, jar2]
    manifest = manifest_builder.build_manifest(jars, "both", mock_logger)  # root removed

    assert len(manifest) == 2
    entry1 = next(e for e in manifest if e["id"] == "mod1")
    assert entry1["file"] == "mod1.jar"  # filename only, not path
    entry2 = next(e for e in manifest if e["id"] == "mod2")
    assert entry2["file"] == "mod2.jar"


def test_build_manifest_outside_root(tmp_path, mock_logger):
    """If jar is outside root, it still uses only the filename (no path)."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    jar = outside / "mod.jar"
    toml = """
[[mods]]
modId = "outside_mod"
version = "1.0"
displayName = "Outside"
"""
    with zipfile.ZipFile(jar, 'w') as zf:
        zf.writestr("META-INF/mods.toml", toml)

    manifest = manifest_builder.build_manifest([jar], "both", mock_logger)
    assert len(manifest) == 1
    assert manifest[0]["file"] == "mod.jar"  # only filename, not absolute path

# ----------------------------------------------------------------------
# Tests for main (via integration-like mocks)
# ----------------------------------------------------------------------

@patch('minecraft.manifest_builder.argparse.ArgumentParser')
@patch('minecraft.manifest_builder.setup_logging')
@patch('minecraft.manifest_builder.get_logger')
@patch('minecraft.manifest_builder.find_jars')
@patch('minecraft.manifest_builder.build_manifest')
@patch('minecraft.manifest_builder.yaml.dump')
def test_main_success(mock_yaml_dump, mock_build_manifest, mock_find_jars,
                      mock_get_logger, mock_setup_logging, mock_parser):
    """Test main() with successful execution."""
    # Setup mocks
    mock_args = MagicMock()
    mock_args.mods = ["mods_dir"]
    mock_args.manifest = Path("config.d/manifest.yaml")
    mock_args.root = Path.cwd()
    mock_args.side = "both"
    mock_args.debug = False
    mock_args.curse = False
    mock_args.resolve = False
    mock_args.env_file = None
    mock_args.no_deduplicate = False
    mock_parser.return_value.parse_args.return_value = mock_args

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    mock_find_jars.return_value = [Path("mods_dir/test.jar")]
    mock_build_manifest.return_value = [{"id": "testmod", "file": "mods/test.jar"}]

    with patch('sys.argv', ['manifest_builder.py']):
        manifest_builder.main()

    mock_setup_logging.assert_called_once()
    mock_logger.info.assert_any_call("Starting manifest builder")
    mock_yaml_dump.assert_called_once_with(
        {"mods": [{"id": "testmod", "file": "mods/test.jar"}]},
        ANY,
        default_flow_style=False,
        sort_keys=False
    )
    mock_logger.info.assert_any_call("Manifest written to config.d/manifest.yaml")


@patch('minecraft.manifest_builder.argparse.ArgumentParser')
@patch('minecraft.manifest_builder.setup_logging')
@patch('minecraft.manifest_builder.get_logger')
@patch('minecraft.manifest_builder.find_jars')
def test_main_no_jars(mock_find_jars, mock_get_logger, mock_setup_logging, mock_parser):
    """main() should exit if no jars found."""
    mock_args = MagicMock()
    mock_args.mods = ["mods_dir"]
    mock_args.manifest = Path("config.d/manifest.yaml")
    mock_args.root = Path.cwd()
    mock_args.side = "both"
    mock_args.debug = False
    mock_args.curse = False
    mock_args.resolve = False
    mock_args.env_file = None
    mock_args.no_deduplicate = False
    mock_parser.return_value.parse_args.return_value = mock_args

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger
    mock_find_jars.return_value = []

    with patch('sys.argv', ['manifest_builder.py']):
        with pytest.raises(SystemExit) as exc:
            manifest_builder.main()
        assert exc.value.code == 1

    mock_logger.error.assert_called_once_with("No .jar files found.")