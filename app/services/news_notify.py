from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def notify_news_dashboard(event: str, payload: dict[str, Any] | None = None) -> None:
    """???? News ????? GitHub Daily ???

    ?? best-effort ????????????? AI Radar ????/?????
    """

    settings = get_settings()
    url = (settings.news_dashboard_webhook_url or "").strip()
    if not url:
        return

    body = {
        "event": event,
        "source": "ai-radar",
        "sent_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **(payload or {}),
    }
    headers: dict[str, str] = {"Content-Type": "application/json"}
    token = (settings.news_dashboard_webhook_token or "").strip()
    if token:
        headers["X-News-Webhook-Token"] = token

    try:
        response = httpx.post(
            url,
            json=body,
            headers=headers,
            timeout=settings.news_dashboard_webhook_timeout_seconds,
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("News ??????? event=%s url=%s error=%s", event, url, exc)
