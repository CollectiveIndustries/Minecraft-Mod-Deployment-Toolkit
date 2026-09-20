# tests/unit/common/test_notify.py

"""Unit tests for notify.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.minecraft.common import notify


@pytest.fixture(autouse=True)
def _silence_notify_logger():
    """Silence notify's logger for every test in this module.

    pytest's captured-log handler can raise a TypeError while
    formatting a log record whose message passes through a MagicMock
    response object (observed on pytest 9.1.1). The notify tests do
    not assert on log output, so we replace the module-level logger
    with a Mock to bypass the formatting path entirely.
    """
    with patch("src.minecraft.common.notify.logger", MagicMock()):
        yield


class TestFitContent:
    """Tests for fitting content within Discord limits."""

    def test_short_content_unchanged(self):
        """Content under the limit is returned unchanged."""
        s = "hello"
        assert notify._fit_content(s) == s

    def test_long_content_truncated_at_boundary(self):
        """Over-length content is trimmed at a blank-line boundary."""
        section = "a" * 800
        content = f"{section}\n\n{section}\n\n{section}"
        out = notify._fit_content(content)
        assert len(out) <= notify.DISCORD_CONTENT_LIMIT
        assert out.endswith(notify.TRUNCATION_MARKER.strip())
        assert out.count(section) == 2

    def test_long_content_no_boundary_falls_back_to_slice(self):
        """Content with no blank lines is hard-sliced to fit the limit."""
        content = "a" * 3000
        out = notify._fit_content(content)
        assert len(out) <= notify.DISCORD_CONTENT_LIMIT
        assert out.endswith(notify.TRUNCATION_MARKER.strip())


class TestPostDiscordWebhook:
    """Tests for post_discord_webhook."""

    def test_empty_url_returns_false_without_request(self):
        """An empty URL short-circuits to False without an HTTP call."""
        with patch("src.minecraft.common.notify.requests.post") as mock_post:
            assert notify.post_discord_webhook("", "hi") is False
        mock_post.assert_not_called()

    def test_success(self):
        """A 2xx response returns True."""
        mock_resp = MagicMock(status_code=204)
        with patch("src.minecraft.common.notify.requests.post", return_value=mock_resp):
            assert notify.post_discord_webhook("https://x.test/hook", "hi") is True

    def test_http_error_returns_false(self):
        """A 4xx response returns False and does not raise."""
        mock_resp = MagicMock(status_code=400)
        mock_resp.text = "bad"
        with patch("src.minecraft.common.notify.requests.post", return_value=mock_resp):
            assert notify.post_discord_webhook("https://x.test/hook", "hi") is False

    def test_request_exception_returns_false(self):
        """A transport error returns False and does not raise."""
        with patch(
            "src.minecraft.common.notify.requests.post",
            side_effect=requests.exceptions.RequestException("boom"),
        ):
            assert notify.post_discord_webhook("https://x.test/hook", "hi") is False

    def test_embeds_capped_at_ten(self):
        """More than ten embeds are truncated to the Discord limit."""
        mock_resp = MagicMock(status_code=204)
        embeds = [{"title": f"e{i}"} for i in range(20)]
        with patch(
            "src.minecraft.common.notify.requests.post",
            return_value=mock_resp,
        ) as mock_post:
            notify.post_discord_webhook("https://x.test/hook", "hi", embeds=embeds)
        _, kwargs = mock_post.call_args
        assert len(kwargs["json"]["embeds"]) == 10
