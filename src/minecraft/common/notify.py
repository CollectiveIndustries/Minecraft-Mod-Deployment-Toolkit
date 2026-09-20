# src/minecraft/common/notify.py

"""Notification helpers for deploy_pack."""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

DISCORD_CONTENT_LIMIT = 2000
TRUNCATION_MARKER = "\n\n... (message truncated)"


def _fit_content(content: str, limit: int = DISCORD_CONTENT_LIMIT) -> str:
    r"""Trim content to fit the Discord limit at a section boundary.

    A "section boundary" is a blank line (\n\n), which matches how the
    webhook template is structured. If no boundary is found inside the
    limit, falls back to a hard slice so a single long paragraph still
    gets sent.
    """
    if len(content) <= limit:
        return content

    budget = limit - len(TRUNCATION_MARKER)
    head = content[:budget]

    boundary = head.rfind("\n\n")
    if boundary > budget // 2:
        head = head[:boundary]

    return head.rstrip() + TRUNCATION_MARKER


def post_discord_webhook(
    webhook_url: str,
    content: str,
    embeds: list[dict[str, Any]] | None = None,
    timeout: float = 10.0,
) -> bool:
    """POST a message to a Discord webhook.

    Returns True on success, False on any failure. Never raises - a
    failed notification must not fail the deploy.
    """
    if not webhook_url:
        return False

    fitted = _fit_content(content)
    if len(content) > DISCORD_CONTENT_LIMIT:
        logger.warning(f"Webhook content exceeded limit ({len(content)} > {DISCORD_CONTENT_LIMIT}), truncating to {len(fitted)}")

    payload: dict[str, Any] = {"content": fitted}
    if embeds:
        payload["embeds"] = embeds[:10]

    try:
        response = requests.post(webhook_url, json=payload, timeout=timeout)
        if response.status_code >= 400:
            status = response.status_code
            body = str(response.text)[:200]
            logger.error(f"Discord webhook returned {status}: {body}")
            return False
        return True
    except RequestException as exc:
        logger.error(f"Discord webhook request failed: {exc}")
        return False
