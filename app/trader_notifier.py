from __future__ import annotations

from typing import Any

import httpx


class TraderNotifier:
    def __init__(self, webhook_url: str | None) -> None:
        self.webhook_url = webhook_url
        self.client = httpx.AsyncClient(timeout=15.0) if webhook_url else None

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()

    async def send(self, title: str, description: str, fields: list[dict[str, Any]] | None = None) -> None:
        if not self.webhook_url or not self.client:
            return
        payload = {
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "fields": fields or [],
                }
            ]
        }
        response = await self.client.post(self.webhook_url, json=payload)
        response.raise_for_status()
