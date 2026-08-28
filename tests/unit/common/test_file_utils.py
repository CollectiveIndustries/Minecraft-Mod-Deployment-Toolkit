# tests/common/test_file_utils.py
import zipfile
from unittest.mock import MagicMock, patch

from src.minecraft.common import file_utils


def test_get_exclude_patterns(temp_dir):
    exclude_file = temp_dir / "exclude.txt"
    exclude_file.write_text("# comment\n*.log\n*.tmp\n")
    patterns = file_utils.get_exclude_patterns(exclude_file, MagicMock())
    assert patterns == ["*.log", "*.tmp"]


def test_get_exclude_patterns_missing(temp_dir):
    patterns = file_utils.get_exclude_patterns(temp_dir / "missing", MagicMock())
    assert patterns == []


def test_copy_directory_contents(temp_dir):
    src = temp_dir / "src"
    src.mkdir()
    (src / "file1.txt").write_text("hello")
    (src / "sub").mkdir()
    (src / "sub" / "file2.txt").write_text("world")
    dst = temp_dir / "dst"
    file_utils.copy_directory_contents(src, dst, MagicMock())
    assert (dst / "file1.txt").exists()
    assert (dst / "sub" / "file2.txt").exists()


def test_copy_with_exclusions(temp_dir):
    src = temp_dir / "src"
    src.mkdir()
    (src / "keep.txt").write_text("keep")
    (src / "skip.log").write_text("skip")
    (src / "sub").mkdir()
    (src / "sub" / "skip.tmp").write_text("skip")
    dst = temp_dir / "dst"
    file_utils.copy_with_exclusions(src, dst, ["*.log", "*.tmp"], MagicMock())
    assert (dst / "keep.txt").exists()
    assert not (dst / "skip.log").exists()
    assert not (dst / "sub" / "skip.tmp").exists()


def test_create_zip_from_staging(temp_dir):
    staging = temp_dir / "staging"
    staging.mkdir()
    (staging / "file1.txt").write_text("data")
    (staging / "sub").mkdir()
    (staging / "sub" / "file2.txt").write_text("data")
    zip_path = temp_dir / "out.zip"
    file_utils.create_zip_from_staging(staging, zip_path, ["*.tmp"], MagicMock())
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "file1.txt" in names
        assert "sub/file2.txt" in names


@patch("src.minecraft.common.file_utils.requests.get")
def test_download_file(mock_get, temp_dir):
    mock_response = MagicMock()
    mock_response.iter_content.return_value = [b"data"]
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response
    out = temp_dir / "downloaded.jar"
    file_utils.download_file("http://example.com/file.jar", out)
    assert out.read_bytes() == b"data"
    mock_get.assert_called_once_with(
        "http://example.com/file.jar", stream=True, timeout=30
    )
