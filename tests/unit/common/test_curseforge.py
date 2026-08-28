# tests/common/test_curseforge.py
from unittest.mock import Mock, patch

from src.minecraft.common import curseforge


@patch("src.minecraft.common.curseforge.requests.request")
def test_curseforge_request(mock_request):
    mock_response = Mock()
    mock_response.json.return_value = {"data": "ok"}
    mock_response.raise_for_status.return_value = None
    mock_request.return_value = mock_response

    result = curseforge.curseforge_request("/v1/test", "api_key", params={"p": 1})
    mock_request.assert_called_once_with(
        "GET",
        "https://api.curseforge.com/v1/test",
        headers={"x-api-key": "api_key", "Accept": "application/json"},
        params={"p": 1},
        json=None,
        timeout=30,
    )
    assert result == {"data": "ok"}


@patch("src.minecraft.common.curseforge.curseforge_request")
def test_get_download_url_by_ids(mock_cf_req):
    mock_cf_req.return_value = {"data": "https://download.url"}
    url = curseforge.get_download_url_by_ids(123, 456, "key")
    assert url == "https://download.url"
    mock_cf_req.assert_called_once_with("/v1/mods/123/files/456/download-url", "key")


@patch("src.minecraft.common.curseforge.curseforge_request")
def test_fetch_all_files(mock_cf_req):
    # Test pagination: two pages
    mock_cf_req.side_effect = [
        {"data": [{"id": 1}, {"id": 2}], "pagination": {"totalCount": 3, "index": 0}},
        {"data": [{"id": 3}], "pagination": {"totalCount": 3, "index": 2}},
    ]
    files = curseforge.fetch_all_files(123, "key", max_pages=2, page_size=2)
    assert len(files) == 3
    assert mock_cf_req.call_count == 2


@patch("src.minecraft.common.curseforge.curseforge_request")
def test_find_mod_by_slug_found(mock_cf_req):
    mock_cf_req.return_value = {"data": [{"id": 1, "slug": "jei"}]}
    mod = curseforge.find_mod_by_slug("jei", "key")
    assert mod == {"id": 1, "slug": "jei"}
    mock_cf_req.assert_called_once_with(
        "/v1/mods/search", "key", params={"gameId": 432, "slug": "jei", "pageSize": 1}
    )


@patch("src.minecraft.common.curseforge.find_mod_by_slug")
@patch("src.minecraft.common.curseforge.fetch_all_files")
@patch("src.minecraft.common.curseforge.get_download_url_by_ids")
def test_get_mod_file_url_by_slug(mock_get_url, mock_fetch, mock_find):
    mock_find.return_value = {"id": 42, "name": "TestMod"}
    mock_fetch.return_value = [
        {
            "id": 100,
            "fileName": "testmod-1.2.3.jar",
            "gameVersions": ["1.20.1"],
            "fileDate": "2023-01-01",
        },
        {
            "id": 101,
            "fileName": "testmod-1.2.4.jar",
            "gameVersions": ["1.20.1"],
            "fileDate": "2023-01-02",
        },
    ]
    mock_get_url.return_value = "https://download.url"
    url, fname = curseforge.get_mod_file_url_by_slug(
        "testmod", "1.2.4", "1.20.1", "key"
    )
    assert url == "https://download.url"
    assert fname == "testmod-1.2.4.jar"
    # It should match by version string in fileName
    mock_get_url.assert_called_once_with(42, 101, "key")
