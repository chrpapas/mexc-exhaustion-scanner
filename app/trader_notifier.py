from __future__ import annotations

import logging
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)


class TraderNotifier:
    def __init__(self, webhook_url: str | None) -> None:
        self.webhook_url = webhook_url
        self.client = httpx.AsyncClient(timeout=15.0) if webhook_url else None

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()

    async def send(
        self,
        title: str,
        description: str,
        fields: list[dict[str, Any]] | None = None,
        *,
        color: int | None = None,
    ) -> bool:
        if not self.webhook_url or not self.client:
            return False
        embed: dict[str, Any] = {
            "title": title[:256],
            "description": description[:4096],
            "fields": (fields or [])[:25],
        }
        if color is not None:
            embed["color"] = color
        try:
            response = await self.client.post(self.webhook_url, json={"embeds": [embed]})
            if response.status_code >= 400:
                LOGGER.error("Trader Discord webhook failed status=%s body=%s", response.status_code, response.text[:500])
            response.raise_for_status()
            return True
        except Exception:
            LOGGER.exception("Trader Discord notification failed")
            return False
