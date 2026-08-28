"""
Additional tests for common.curseforge to cover error paths and edge cases.
"""

from unittest.mock import patch

import pytest
import requests

from src.minecraft.common import curseforge


@patch("src.minecraft.common.curseforge.curseforge_request")
def test_find_mod_by_slug_not_found(mock_cf_req):
    """find_mod_by_slug should return None if no mod found."""
    mock_cf_req.return_value = {"data": []}
    result = curseforge.find_mod_by_slug("unknown", "key")
    assert result is None


@patch("src.minecraft.common.curseforge.curseforge_request")
def test_find_mod_by_slug_request_error(mock_cf_req):
    """find_mod_by_slug should return None on RequestException."""
    mock_cf_req.side_effect = requests.RequestException("Network error")
    result = curseforge.find_mod_by_slug("any", "key")
    assert result is None


def test_get_mod_file_url_by_slug_fallback_search():
    """When find_mod_by_slug returns None, fallback to searchFilter."""
    with (
        patch("src.minecraft.common.curseforge.find_mod_by_slug", return_value=None),
        patch("src.minecraft.common.curseforge.curseforge_request") as mock_req,
        patch("src.minecraft.common.curseforge.fetch_all_files") as mock_fetch,
        patch("src.minecraft.common.curseforge.get_download_url_by_ids") as mock_dl,
    ):
        mock_req.return_value = {"data": [{"id": 123, "name": "TestMod"}]}
        mock_fetch.return_value = [
            {
                "id": 456,
                "fileName": "test-1.0.jar",
                "gameVersions": ["1.20.1"],
                "fileDate": "2023-01-01",
            }
        ]
        mock_dl.return_value = "http://example.com/test.jar"

        url, fname = curseforge.get_mod_file_url_by_slug(
            "testmod", "1.0", "1.20.1", "key"
        )
        assert url == "http://example.com/test.jar"
        assert fname == "test-1.0.jar"


@patch("src.minecraft.common.curseforge.curseforge_request")
@patch("src.minecraft.common.curseforge.fetch_all_files")
def test_get_mod_file_url_by_slug_no_files(mock_fetch, mock_cf_req):
    """Should raise ValueError if no files found."""
    with patch("src.minecraft.common.curseforge.find_mod_by_slug") as mock_find:
        mock_find.return_value = {"id": 123, "name": "TestMod"}
        mock_fetch.return_value = []
        with pytest.raises(ValueError, match="No files found for mod 'testmod'"):
            curseforge.get_mod_file_url_by_slug("testmod", "1.0", "1.20.1", "key")


@patch("src.minecraft.common.curseforge.get_download_url_by_ids")
@patch("src.minecraft.common.curseforge.fetch_all_files")
def test_get_mod_file_url_by_slug_fallback_latest(mock_fetch, mock_dl):
    """When no version match, should fallback to the latest file."""
    with patch("src.minecraft.common.curseforge.find_mod_by_slug") as mock_find:
        mock_find.return_value = {"id": 123, "name": "TestMod"}
        mock_fetch.return_value = [
            {
                "id": 456,
                "fileName": "testmod-2.0.jar",
                "gameVersions": ["1.20.1"],
                "fileDate": "2023-02-01",
            },
            {
                "id": 457,
                "fileName": "testmod-1.5.jar",
                "gameVersions": ["1.20.1"],
                "fileDate": "2023-01-15",
            },
        ]
        mock_dl.return_value = "http://example.com/test-2.0.jar"
        url, fname = curseforge.get_mod_file_url_by_slug(
            "testmod", "1.0", "1.20.1", "key"
        )
        assert url == "http://example.com/test-2.0.jar"
        assert fname == "testmod-2.0.jar"
        mock_dl.assert_called_once_with(123, 456, "key")


@patch("src.minecraft.common.curseforge.find_mod_by_slug")
def test_get_mod_file_url_by_slug_exact_filename(mock_find):
    """Should match by exact filename (case-insensitive)."""
    mock_find.return_value = {"id": 123, "name": "TestMod"}
    with patch("src.minecraft.common.curseforge.fetch_all_files") as mock_fetch:
        mock_fetch.return_value = [
            {
                "id": 456,
                "fileName": "testmod.jar",
                "gameVersions": ["1.20.1"],
                "fileDate": "2023-01-01",
            }
        ]
        with patch(
            "src.minecraft.common.curseforge.get_download_url_by_ids"
        ) as mock_dl:
            mock_dl.return_value = "http://example.com/test.jar"
            url, fname = curseforge.get_mod_file_url_by_slug(
                "testmod", "1.0", "1.20.1", "key"
            )
            assert url == "http://example.com/test.jar"
            assert fname == "testmod.jar"
