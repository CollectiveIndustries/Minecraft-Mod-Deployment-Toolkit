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
        1. .env file (if provided) - loaded as a flat file source with
           literal keys, exactly as written. Keys must match the names the
           application reads (e.g. ``webhook_url``); no prefix stripping
           or dot expansion is applied to file sources.
        2. Config file: config_dir / f"{base_name}.toml" (or yaml/yml)
        3. Environment variables with the given prefix - stripped, lowercased,
           and dot-expanded into nested keys (PREFIX_DATABASE_HOST becomes
           ``database.host``). Not used by keys that also live in the .env
           file unless you set the real env var.
        4. CLI arguments (--key value or --key=value)
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
