# tests/common/test_config.py
import pytest

from src.minecraft.common import config


def test_find_config_file_found(temp_dir):
    (temp_dir / "deploy_pack.toml").touch()
    (temp_dir / "deploy_pack.yaml").touch()
    result = config.find_config_file(temp_dir, "deploy_pack")
    assert result == temp_dir / "deploy_pack.toml"


def test_find_config_file_not_found(temp_dir):
    result = config.find_config_file(temp_dir, "deploy_pack")
    assert result is None


def test_load_combined_config(mock_config_manager, temp_dir):
    # We no longer use mock_logging; we patch load_dotenv inside the test if needed.
    (temp_dir / "deploy_pack.toml").write_text("key = 'value'")
    mock_config_manager.load.return_value = {"key": "value", "other": "env"}
    loaded = config.load_combined_config(
        config_dir=temp_dir,
        base_name="deploy_pack",
        env_prefix="DEPLOYPACK",
        cli_args=["--foo=bar"],
        env_file_candidates=[temp_dir / ".env"],
    )
    assert loaded == {"key": "value", "other": "env"}
    mock_config_manager.file.assert_called_once()
    mock_config_manager.env.assert_called_once_with("DEPLOYPACK")
    mock_config_manager.cli.assert_called_once_with(["--foo=bar"])


def test_get_required_config_value():
    config_dict = {"a": "1", "b": None}
    assert config.get_required_config_value(config_dict, "a", "") == "1"
    with pytest.raises(ValueError, match="Missing required configuration key: b"):
        config.get_required_config_value(config_dict, "b", "error msg")
