# tests/common/test_manifest.py

import pytest
import yaml

from src.minecraft.common import manifest


def test_load_manifest(temp_dir, sample_manifest_data):
    manifest_path = temp_dir / "manifest.yaml"
    with manifest_path.open("w") as f:
        yaml.dump(sample_manifest_data, f)
    mods = manifest.load_manifest(manifest_path)
    assert len(mods) == 3
    assert mods[0]["id"] == "mod1"


def test_load_manifest_not_found(temp_dir):
    with pytest.raises(FileNotFoundError):
        manifest.load_manifest(temp_dir / "missing.yaml")


def test_filter_mods_by_side(sample_manifest_data):
    mods = sample_manifest_data["mods"]
    client_mods = manifest.filter_mods_by_side(mods, "client")
    assert len(client_mods) == 2
    ids = [m["id"] for m in client_mods]
    assert "mod1" in ids
    assert "mod2" in ids
    assert "mod3" not in ids

    server_mods = manifest.filter_mods_by_side(mods, "server")
    assert len(server_mods) == 2
    ids = [m["id"] for m in server_mods]
    assert "mod1" in ids
    assert "mod3" in ids
    assert "mod2" not in ids
