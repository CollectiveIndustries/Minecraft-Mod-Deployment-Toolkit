"""Unit tests for deploy_pack.py."""

from unittest.mock import MagicMock, patch

import pytest

from src.minecraft import deploy_pack


def test_main_client_mode_missing_instance_name():
    """Client mode should exit if instance_name is missing."""
    with (
        patch("src.minecraft.deploy_pack.cfg.load_config") as mock_load_config,
        patch("src.minecraft.deploy_pack.load_mod_list") as mock_load_mod_list,
    ):
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: (
            {"sync_root": "/fake", "live_server": "/fake", "www_dir": "/fake"}.get(
                key, default
            )
            if key != "instance_name"
            else None
        )
        mock_config.as_dict.return_value = {}
        mock_load_config.return_value = mock_config
        # Return an empty list so that the staging step sees no mods
        mock_load_mod_list.return_value = []

        with (
            patch("src.minecraft.deploy_pack.setup_logging"),
            patch("src.minecraft.deploy_pack.get_logger") as mock_get_logger,
            patch("src.minecraft.deploy_pack.tempfile.TemporaryDirectory"),
            patch(
                "sys.argv",
                ["deploy_pack.py", "--client", "--prism-index", "/fake"],
            ),
        ):
            mock_logger = MagicMock()
            mock_get_logger.return_value = mock_logger
            with pytest.raises(SystemExit) as exc:
                deploy_pack.main()
            # Our code exits with 1 when instance_name is missing
            assert exc.value.code == 1
            mock_logger.error.assert_called_once_with(
                "instance_name must be set for client mode"
            )
