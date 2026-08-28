# src/minecraft/common/config.py
"""Configuration loading, environment setup, and ConfigManager wrappers."""

from pathlib import Path

from ConfigCore import ConfigManager
from dotenv import load_dotenv


def find_config_file(config_dir: Path, base_name: str = "deploy_pack") -> Path | None:
    """
    Return the path to the first existing config file in config_dir.
    Searches for {base_name}.toml, .yaml, .yml.
    """
    for ext in (".toml", ".yaml", ".yml"):
        candidate = config_dir / f"{base_name}{ext}"
        if candidate.is_file():
            return candidate
    return None


def load_combined_config(
    config_dir: Path,
    base_name: str,
    env_prefix: str,
    env_file_candidates: list[Path] | None = None,
    cli_args: list[str] | None = None,
) -> dict:
    """
    Load configuration from:
      1. .env files (via python-dotenv) – added to os.environ
      2. Config file (TOML/YAML) from config_dir
      3. Environment variables with prefix env_prefix
      4. Command-line arguments (as key=value pairs) if provided.

    Returns a dictionary with the merged configuration.
    """
    # Load .env files
    if env_file_candidates is None:
        env_file_candidates = [config_dir / ".env", Path(".env")]
    for env_file in env_file_candidates:
        if env_file.is_file():
            load_dotenv(dotenv_path=env_file)
            break

    mgr = ConfigManager()

    config_path = find_config_file(config_dir, base_name)
    if config_path is not None:
        mgr.file(config_path)

    if env_prefix:
        mgr.env(env_prefix)

    if cli_args:
        mgr.cli(cli_args)

    return mgr.load()


def get_required_config_value(config: dict, key: str, error_msg: str) -> str:
    """Get a required config value, exit with error if missing."""
    value = config.get(key)
    if value is None:
        raise ValueError(f"Missing required configuration key: {key}. {error_msg}")
    return str(value)
