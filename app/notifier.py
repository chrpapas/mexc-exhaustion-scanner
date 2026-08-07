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

    async def send_signal(self, signal: RunSignal) -> None:
        if not self._webhook_url:
            return

        features = signal.features
        run_score = features.get("run_score", signal.score)
        exhaustion_score = features.get("exhaustion_score")
        if signal.level == "short_setup":
            title = f"🚨 **{signal.symbol} — SHORT SETUP**"
        elif signal.level == "exhaustion_watch":
            title = f"🟠 **{signal.symbol} — EXHAUSTION WATCH**"
        else:
            title = f"🟡 **{signal.symbol} — RUN WATCH**"

        lines = [
            title,
            f"Run score: {run_score}/6",
            f"24h: {self._percent(features.get('return_24h'))}",
            f"72h: {self._percent(features.get('return_72h'))}",
            f"BTC residual: {self._percent(features.get('residual_return_24h'))}",
            f"1h momentum: {self._percent(features.get('momentum_1h'))}",
            f"Volume z-score: {self._number(features.get('volume_zscore_15m'))}",
            f"EMA distance: {self._number(features.get('distance_above_ema20_atr_4h'))} ATR",
            f"Funding: {self._percent(features.get('funding_rate'))}",
        ]
        if signal.level in {"exhaustion_watch", "short_setup"}:
            lines.append(f"Exhaustion score: {exhaustion_score if exhaustion_score is not None else 'n/a'}/7")
        if signal.level == "short_setup":
            lines.append(
                f"Structural break: {'YES' if features.get('structural_break_15m') else 'NO'}"
            )

        lines.extend(
            [
                "Reasons: " + "; ".join(signal.reasons),
                "Shadow mode only — no order is placed.",
            ]
        )
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
