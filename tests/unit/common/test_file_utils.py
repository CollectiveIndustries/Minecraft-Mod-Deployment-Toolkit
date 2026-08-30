# tests/unit/common/test_file_utils.py
"""Unit tests for file utility functions."""

import hashlib
from unittest.mock import patch

import pytest
from requests.exceptions import RequestException

from src.minecraft.common import file_utils


class DummyLogger:
    """A logger that does nothing (avoids formatting issues in tests)."""

    def debug(self, msg, *args, **kwargs):
        pass

    def info(self, msg, *args, **kwargs):
        pass

    def warning(self, msg, *args, **kwargs):
        pass

    def error(self, msg, *args, **kwargs):
        pass


@pytest.fixture
def dummy_logger():
    """Provide a dummy logger that does nothing."""
    return DummyLogger()


def test_get_exclude_patterns(tmp_path, dummy_logger):
    exclude_file = tmp_path / ".rsync_exclude"
    exclude_file.write_text("*.tmp\n# comment\n*.log\n")
    patterns = file_utils.get_exclude_patterns(exclude_file, dummy_logger)
    assert patterns == ["*.tmp", "*.log"]

    # Missing file -> empty list
    patterns = file_utils.get_exclude_patterns(tmp_path / "missing", dummy_logger)
    assert patterns == []


def test_copy_directory_contents(tmp_path, dummy_logger):
    src = tmp_path / "src"
    src.mkdir()
    (src / "file1.txt").write_text("content")
    sub = src / "sub"
    sub.mkdir()
    (sub / "file2.txt").write_text("more")
    dst = tmp_path / "dst"
    file_utils.copy_directory_contents(src, dst, dummy_logger)
    assert (dst / "file1.txt").exists()
    assert (dst / "sub" / "file2.txt").exists()


def test_copy_with_exclusions(tmp_path, dummy_logger):
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep.txt").write_text("keep")
    (src / "skip.log").write_text("skip")
    dst = tmp_path / "dst"
    file_utils.copy_with_exclusions(src, dst, ["*.log"], dummy_logger)
    assert (dst / "keep.txt").exists()
    assert not (dst / "skip.log").exists()


def test_create_zip_from_staging(tmp_path, dummy_logger):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "file.txt").write_text("test")
    sub = staging / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested")
    zip_path = tmp_path / "out.zip"
    file_utils.create_zip_from_staging(staging, zip_path, [], dummy_logger)
    assert zip_path.is_file()


def test_download_file(tmp_path):
    out = tmp_path / "downloaded.dat"
    with patch("requests.get") as mock_get:
        mock_response = mock_get.return_value
        mock_response.iter_content.return_value = [b"data"]
        mock_response.raise_for_status.return_value = None
        file_utils.download_file("http://example.com/file.dat", out)
    assert out.read_bytes() == b"data"


def test_compute_file_hash(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("hello")
    expected = hashlib.sha512(b"hello").hexdigest()
    assert file_utils.compute_file_hash(f) == expected
    assert (
        file_utils.compute_file_hash(f, "sha256")
        == hashlib.sha256(b"hello").hexdigest()
    )


def test_verify_file_hash(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("hello")
    correct = hashlib.sha512(b"hello").hexdigest()
    assert file_utils.verify_file_hash(f, correct) is True
    assert file_utils.verify_file_hash(f, "wrong") is False
    missing = tmp_path / "missing"
    assert file_utils.verify_file_hash(missing, correct) is False


def test_ensure_mod_file_missing_no_download(tmp_path, dummy_logger):
    modpath = tmp_path / "mod.jar"
    result = file_utils.ensure_mod_file(modpath, None, None, logger=dummy_logger)
    assert result is False


def test_ensure_mod_file_exists_no_hash(tmp_path, dummy_logger):
    modpath = tmp_path / "mod.jar"
    modpath.write_text("content")
    result = file_utils.ensure_mod_file(modpath, None, None, logger=dummy_logger)
    assert result is True


def test_ensure_mod_file_missing_with_download(tmp_path, dummy_logger):
    modpath = tmp_path / "mod.jar"
    url = "http://example.com/mod.jar"
    with patch("src.minecraft.common.file_utils.download_file") as mock_download:

        def fake_download(url, out):
            out.write_text("downloaded content")

        mock_download.side_effect = fake_download
        result = file_utils.ensure_mod_file(modpath, url, None, logger=dummy_logger)
    assert result is True
    assert modpath.read_text() == "downloaded content"


def test_ensure_mod_file_hash_mismatch_download_retry(tmp_path, dummy_logger):
    modpath = tmp_path / "mod.jar"
    url = "http://example.com/mod.jar"
    expected_hash = hashlib.sha512(b"correct").hexdigest()
    # Initially file exists but wrong content
    modpath.write_text("wrong")
    with patch("src.minecraft.common.file_utils.download_file") as mock_download:

        def fake_download(url, out):
            out.write_text("correct")

        mock_download.side_effect = fake_download
        result = file_utils.ensure_mod_file(
            modpath, url, expected_hash, logger=dummy_logger
        )
    assert result is True
    assert file_utils.verify_file_hash(modpath, expected_hash) is True


def test_ensure_mod_file_download_fails(tmp_path, dummy_logger):
    modpath = tmp_path / "mod.jar"
    url = "http://example.com/mod.jar"
    expected_hash = hashlib.sha512(b"correct").hexdigest()
    with patch(
        "src.minecraft.common.file_utils.download_file",
        side_effect=RequestException("Network error"),
    ):
        result = file_utils.ensure_mod_file(
            modpath, url, expected_hash, logger=dummy_logger
        )
    assert result is False
    assert not modpath.exists()
