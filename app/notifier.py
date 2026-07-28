from __future__ import annotations

import logging

import httpx

from app.models import RunSignal

LOGGER = logging.getLogger(__name__)


class DiscordNotifier:
    def __init__(self, webhook_url: str | None) -> None:
        self._webhook_url = webhook_url
        self._client = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def send_run_candidate(self, signal: RunSignal) -> None:
        if not self._webhook_url:
            return
        features = signal.features
        lines = [
            f"**{signal.symbol} — abnormal run candidate (score {signal.score}/6)**",
            f"24h: {self._percent(features.get('return_24h'))}",
            f"72h: {self._percent(features.get('return_72h'))}",
            f"BTC residual: {self._percent(features.get('residual_return_24h'))}",
            f"Volume z-score: {self._number(features.get('volume_zscore_15m'))}",
            f"EMA distance: {self._number(features.get('distance_above_ema20_atr_4h'))} ATR",
            f"Funding: {self._percent(features.get('funding_rate'))}",
            "Reasons: " + "; ".join(signal.reasons),
            "Shadow mode only — this is not a short-entry signal.",
        ]
        try:
            response = await self._client.post(self._webhook_url, json={"content": "\n".join(lines)})
            response.raise_for_status()
        except httpx.HTTPError:
            LOGGER.exception("Discord alert failed for %s", signal.symbol)

    @staticmethod
    def _percent(value: object) -> str:
        return "n/a" if value is None else f"{float(value):.2%}"

    @staticmethod
    def _number(value: object) -> str:
        return "n/a" if value is None else f"{float(value):.2f}"
