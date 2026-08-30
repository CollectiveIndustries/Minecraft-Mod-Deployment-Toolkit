# src/minecraft/common/config.py
"""Configuration loading using ConfigCore."""

from pathlib import Path

from ConfigCore import Config, ConfigManager


def load_config(
    config_dir: Path,
    base_name: str = "deploy_pack",
    env_prefix: str = "",
    cli_args: list[str] | None = None,
    env_file: Path | None = None,
) -> Config:
    """Load configuration using ConfigCore.

    Sources (in order of increasing priority):
        1. .env file (if provided) - loaded as environment variables
        2. Config file: config_dir / f"{base_name}.toml" (or yaml/yml)
        3. Environment variables with the given prefix
        4. CLI arguments (--key value or --key=value)

    Returns a Config object (dot-notation access).
    """
    # Build the ConfigManager
    mgr = ConfigManager()

    # 1. Load .env file if provided (loads into environment, but we can also load as a file source)
    if env_file and env_file.is_file():
        mgr.file(env_file, format="env")

    # 2. Load main config file (auto-detect extension)
    config_path = find_config_file(config_dir, base_name)
    if config_path:
        mgr.file(config_path)

    # 3. Environment variables with prefix
    if env_prefix:
        mgr.env(env_prefix)

    # 4. CLI arguments
    if cli_args:
        mgr.cli(cli_args)

    # Load and return Config object
    return mgr.load()


def find_config_file(config_dir: Path, base_name: str) -> Path | None:
    """Locate the first existing config file: {base_name}.toml, .yaml, .yml."""
    for ext in (".toml", ".yaml", ".yml"):
        candidate = config_dir / f"{base_name}{ext}"
        if candidate.is_file():
            return candidate
    return None
