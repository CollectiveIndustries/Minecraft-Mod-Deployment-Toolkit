"""
Unit tests for manifest_builder.py - adapted to new common modules.
"""

import zipfile
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from minecraft import manifest_builder

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def mock_logger():
    with patch("minecraft.manifest_builder.get_logger") as mock_get:
        logger = MagicMock()
        mock_get.return_value = logger
        yield logger


# ----------------------------------------------------------------------
# Tests for find_jars
# ----------------------------------------------------------------------


def test_find_jars(tmp_path, mock_logger):
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
    mock_logger.warning.assert_not_called()


def test_find_jars_missing_dir(tmp_path, mock_logger):
    missing = tmp_path / "missing"
    jars = list(manifest_builder.find_jars([missing], mock_logger))
    assert jars == []
    mock_logger.warning.assert_called_once_with(
        f"{missing} is not a directory, skipping"
    )


# ----------------------------------------------------------------------
# Tests for extract_metadata
# ----------------------------------------------------------------------


def test_extract_metadata_success(tmp_path, mock_logger):
    jar_path = tmp_path / "test.jar"
    toml_content = """
[[mods]]
modId = "testmod"
version = "1.0.0"
displayName = "Test Mod"
side = "CLIENT"

[[mods.dependencies]]
modId = "dep1"
mandatory = true
"""
    with zipfile.ZipFile(jar_path, "w") as zf:
        zf.writestr("META-INF/mods.toml", toml_content)

    result = manifest_builder.extract_metadata(jar_path, mock_logger)
    assert result == {
        "id": "testmod",
        "version": "1.0.0",
        "display_name": "Test Mod",
        "side": "client",
        "depends": ["dep1"],
    }


def test_extract_metadata_no_toml(tmp_path, mock_logger):
    jar_path = tmp_path / "test.jar"
    with zipfile.ZipFile(jar_path, "w") as zf:
        zf.writestr("META-INF/other.txt", "dummy")
    result = manifest_builder.extract_metadata(jar_path, mock_logger)
    assert result is None
    mock_logger.debug.assert_called_once_with(f"No metadata file found in {jar_path}")


def test_extract_metadata_neoforge_toml(tmp_path, mock_logger):
    jar_path = tmp_path / "test.jar"
    toml_content = """
[[mods]]
modId = "neoforge_mod"
version = "2.0.0"
displayName = "NeoForge Mod"
side = "SERVER"
"""
    with zipfile.ZipFile(jar_path, "w") as zf:
        zf.writestr("META-INF/neoforge.mods.toml", toml_content)
    result = manifest_builder.extract_metadata(jar_path, mock_logger)
    assert result["id"] == "neoforge_mod"
    assert result["side"] == "server"


def test_extract_metadata_corrupt_toml(tmp_path, mock_logger):
    jar_path = tmp_path / "test.jar"
    with zipfile.ZipFile(jar_path, "w") as zf:
        zf.writestr("META-INF/mods.toml", "this is not toml [")
    result = manifest_builder.extract_metadata(jar_path, mock_logger)
    assert result is None
    mock_logger.error.assert_called_once()


# ----------------------------------------------------------------------
# Tests for build_manifest_from_jars
# ----------------------------------------------------------------------


def test_build_manifest_from_jars(tmp_path, mock_logger):
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
    with zipfile.ZipFile(jar1, "w") as zf:
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
    with zipfile.ZipFile(jar2, "w") as zf:
        zf.writestr("META-INF/mods.toml", toml2)

    jars = [jar1, jar2]
    manifest = manifest_builder.build_manifest_from_jars(jars, "both", mock_logger)
    assert len(manifest) == 2
    entry1 = next(e for e in manifest if e["id"] == "mod1")
    assert entry1["file"] == "mod1.jar"
    entry2 = next(e for e in manifest if e["id"] == "mod2")
    assert entry2["file"] == "mod2.jar"


def test_build_manifest_from_jars_outside_root(tmp_path, mock_logger):
    outside = tmp_path / "outside"
    outside.mkdir()
    jar = outside / "mod.jar"
    toml = """
[[mods]]
modId = "outside_mod"
version = "1.0"
displayName = "Outside"
"""
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("META-INF/mods.toml", toml)
    manifest = manifest_builder.build_manifest_from_jars([jar], "both", mock_logger)
    assert len(manifest) == 1
    assert manifest[0]["file"] == "mod.jar"


def test_build_manifest_from_jars_curse(tmp_path, mock_logger):
    root = tmp_path / "root"
    root.mkdir()
    jar = root / "mod.jar"
    toml = """
[[mods]]
modId = "testmod"
version = "1.0"
displayName = "Test Mod"
"""
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("META-INF/mods.toml", toml)

    entries = manifest_builder.build_manifest_from_jars(
        [jar], "both", mock_logger, curse=True
    )
    assert len(entries) == 1
    assert entries[0]["source"] == "curseforge"
    assert entries[0]["slug"] == "testmod"


@patch("minecraft.manifest_builder.resolve_by_search_direct")
def test_build_manifest_from_jars_resolve(mock_resolve, tmp_path, mock_logger):
    root = tmp_path / "root"
    root.mkdir()
    jar = root / "mod.jar"
    toml = """
[[mods]]
modId = "testmod"
version = "1.0"
displayName = "Test Mod"
"""
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("META-INF/mods.toml", toml)

    mock_resolve.return_value = {jar: (123, 456)}
    entries = manifest_builder.build_manifest_from_jars(
        [jar], "both", mock_logger, curse=True, resolve=True, api_key="fake"
    )
    assert len(entries) == 1
    assert entries[0]["project_id"] == 123
    assert entries[0]["file_id"] == 456


def test_build_manifest_from_jars_dedup(tmp_path, mock_logger):
    root = tmp_path / "root"
    root.mkdir()
    jar1 = root / "mod1.jar"
    jar2 = root / "mod2.jar"
    toml = """
[[mods]]
modId = "testmod"
version = "1.0"
displayName = "Test Mod"
"""
    with zipfile.ZipFile(jar1, "w") as zf:
        zf.writestr("META-INF/mods.toml", toml)
    with zipfile.ZipFile(jar2, "w") as zf:
        zf.writestr("META-INF/mods.toml", toml)

    entries = manifest_builder.build_manifest_from_jars(
        [jar1, jar2], "both", mock_logger
    )
    assert len(entries) == 1
    assert entries[0]["file"] == "mod1.jar"


# ----------------------------------------------------------------------
# Tests for main()
# ----------------------------------------------------------------------


@patch("minecraft.manifest_builder.argparse.ArgumentParser")
@patch("minecraft.manifest_builder.setup_logging")
@patch("minecraft.manifest_builder.get_logger")
@patch("minecraft.manifest_builder.find_jars")
@patch("minecraft.manifest_builder.build_manifest_from_jars")
@patch("minecraft.manifest_builder.yaml.dump")
def test_main_success(
    mock_yaml_dump,
    mock_build_manifest,
    mock_find_jars,
    mock_get_logger,
    mock_setup_logging,
    mock_parser,
):
    mock_args = MagicMock()
    mock_args.mods = ["mods_dir"]
    mock_args.manifest = Path("config.d/manifest.yaml")
    mock_args.side = "both"
    mock_args.debug = False
    mock_args.curse = False
    mock_args.resolve = False
    mock_args.env_file = None
    mock_args.no_deduplicate = False
    mock_args.prism_index = None
    mock_parser.return_value.parse_args.return_value = mock_args

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    mock_find_jars.return_value = [Path("mods_dir/test.jar")]
    mock_build_manifest.return_value = [{"id": "testmod", "file": "mods/test.jar"}]

    with patch("sys.argv", ["manifest_builder.py"]):
        manifest_builder.main()

    mock_yaml_dump.assert_called_once_with(
        {"mods": [{"id": "testmod", "file": "mods/test.jar"}]},
        ANY,
        default_flow_style=False,
        sort_keys=False,
    )
    mock_logger.info.assert_any_call("Manifest written to config.d/manifest.yaml")


@patch("minecraft.manifest_builder.argparse.ArgumentParser")
@patch("minecraft.manifest_builder.setup_logging")
@patch("minecraft.manifest_builder.get_logger")
@patch("minecraft.manifest_builder.find_jars")
def test_main_no_jars(mock_find_jars, mock_get_logger, mock_setup_logging, mock_parser):
    # Provide --mods to trigger JAR mode
    mock_args = MagicMock()
    mock_args.mods = ["mods_dir"]
    mock_args.manifest = Path("config.d/manifest.yaml")
    mock_args.side = "both"
    mock_args.debug = False
    mock_args.curse = False
    mock_args.resolve = False
    mock_args.env_file = None
    mock_args.no_deduplicate = False
    mock_args.prism_index = None
    mock_parser.return_value.parse_args.return_value = mock_args

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger
    mock_find_jars.return_value = []

    with patch("sys.argv", ["manifest_builder.py"]), pytest.raises(SystemExit) as exc:
        manifest_builder.main()
    assert exc.value.code == 1
    mock_logger.error.assert_called_once_with("No .jar files found.")


@patch("minecraft.manifest_builder.argparse.ArgumentParser")
@patch("minecraft.manifest_builder.setup_logging")
@patch("minecraft.manifest_builder.get_logger")
@patch("minecraft.manifest_builder.find_jars")
@patch("minecraft.manifest_builder.build_manifest_from_jars")
@patch("minecraft.manifest_builder.yaml.dump")
@patch("minecraft.manifest_builder.cfg.load_combined_config")
def test_main_with_resolve(
    mock_load_config,
    mock_yaml_dump,
    mock_build_manifest,
    mock_find_jars,
    mock_get_logger,
    mock_setup_logging,
    mock_parser,
):
    mock_args = MagicMock()
    mock_args.mods = ["mods_dir"]
    mock_args.manifest = Path("config.d/manifest.yaml")
    mock_args.side = "both"
    mock_args.debug = False
    mock_args.curse = False
    mock_args.resolve = True
    mock_args.env_file = None
    mock_args.no_deduplicate = False
    mock_args.prism_index = None
    mock_parser.return_value.parse_args.return_value = mock_args

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger
    mock_find_jars.return_value = [Path("mods_dir/test.jar")]
    mock_build_manifest.return_value = [{"id": "testmod", "file": "mods/test.jar"}]

    # Mock config loading to return an API key
    mock_config = MagicMock()
    mock_config.get.side_effect = lambda key, default=None: (
        "fake_key" if key in ["CF_API_KEY", "api_key"] else default
    )
    mock_load_config.return_value = mock_config

    with patch("sys.argv", ["manifest_builder.py"]):
        manifest_builder.main()

    mock_build_manifest.assert_called_once_with(
        ANY, "both", mock_logger, curse=True, resolve=True, api_key="fake_key"
    )
    # The internal args.curse becomes True because resolve=True implies curse=True.
    # We don't need to check the log message; the build_manifest call confirms it.


@patch("minecraft.manifest_builder.argparse.ArgumentParser")
@patch("minecraft.manifest_builder.setup_logging")
@patch("minecraft.manifest_builder.get_logger")
@patch("minecraft.manifest_builder.find_jars")
def test_main_resolve_no_key(
    mock_find_jars, mock_get_logger, mock_setup_logging, mock_parser
):
    # Need to pass --mods to force JAR mode
    mock_args = MagicMock()
    mock_args.mods = ["mods_dir"]
    mock_args.manifest = Path("config.d/manifest.yaml")
    mock_args.side = "both"
    mock_args.debug = False
    mock_args.curse = False
    mock_args.resolve = True
    mock_args.env_file = None
    mock_args.no_deduplicate = False
    mock_args.prism_index = None
    mock_parser.return_value.parse_args.return_value = mock_args

    mock_logger = MagicMock()
    mock_get_logger.return_value = mock_logger

    # Mock config loader to return no API key
    with patch("minecraft.manifest_builder.cfg.load_combined_config") as mock_load:
        mock_load.return_value = {}
        with (
            patch("sys.argv", ["manifest_builder.py"]),
            pytest.raises(SystemExit) as exc,
        ):
            manifest_builder.main()
        assert exc.value.code == 1

    mock_logger.error.assert_called_once_with(
        "CF_API_KEY not found; cannot resolve CurseForge IDs."
    )


# ----------------------------------------------------------------------
# Tests for Prism index builder
# ----------------------------------------------------------------------


def test_build_manifest_from_prism(tmp_path, mock_logger):
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    # Write a valid .pw.toml
    toml_content = """
filename = "testmod.jar"
name = "Test Mod"
side = "server"
[update.curseforge]
project-id = 123
file-id = 456
"""
    (index_dir / "testmod.pw.toml").write_text(toml_content)
    entries = manifest_builder.build_manifest_from_prism(index_dir, mock_logger)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["id"] == "123"
    assert entry["file"] == "testmod.jar"
    assert entry["side"] == "server"
    assert entry["project_id"] == 123
    assert entry["file_id"] == 456


def test_build_manifest_from_prism_skips_non_cf(tmp_path, mock_logger):
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    # Write a .pw.toml without CurseForge update (e.g., Modrinth)
    toml_content = """
filename = "modrinth_mod.jar"
name = "Modrinth Mod"
[update.modrinth]
mod-id = "xyz"
version = "abc"
"""
    (index_dir / "modrinth.pw.toml").write_text(toml_content)
    entries = manifest_builder.build_manifest_from_prism(index_dir, mock_logger)
    assert len(entries) == 0
    mock_logger.warning.assert_called_once_with(
        "Skipping modrinth.pw.toml (missing CurseForge data or unsupported)"
    )
