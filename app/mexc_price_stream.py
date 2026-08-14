from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from websockets.asyncio.client import connect

LOGGER = logging.getLogger(__name__)


def ticker_price_from_message(message: str | bytes, symbol: str) -> float | None:
    """Return lastPrice from a MEXC push.ticker message for *symbol*."""
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    try:
        payload = json.loads(message)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("channel") != "push.ticker":
        return None
    payload_symbol = str(payload.get("symbol") or "")
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    data_symbol = str(data.get("symbol") or payload_symbol)
    if data_symbol != symbol:
        return None
    raw_price = data.get("lastPrice")
    if raw_price in {None, ""}:
        return None
    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


class MexcTickerStream:
    """Single-symbol MEXC futures ticker stream for the one-position trader."""

    def __init__(self, url: str = "wss://contract.mexc.com/edge") -> None:
        self.url = url
        self._symbol: str | None = None
        self._price: float | None = None
        self._updated_at = 0.0
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._lock:
            task = self._task
            self._task = None
            self._symbol = None
            self._ready.clear()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def last_price(
        self,
        symbol: str,
        *,
        timeout_seconds: float = 4.0,
        max_age_seconds: float = 5.0,
    ) -> float:
        await self._ensure_symbol(symbol)
        if self._fresh(max_age_seconds):
            assert self._price is not None
            return self._price
        self._ready.clear()
        # Re-check after clearing so a price arriving between the first freshness
        # check and Event.clear() cannot be lost.
        if self._fresh(max_age_seconds):
            assert self._price is not None
            return self._price
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise RuntimeError(f"MEXC ticker WebSocket timed out for {symbol}") from exc
        if self._price is None:
            raise RuntimeError(f"MEXC ticker WebSocket has no price for {symbol}")
        return self._price

    def _fresh(self, max_age_seconds: float) -> bool:
        return self._price is not None and (time.monotonic() - self._updated_at) <= max_age_seconds

    async def _ensure_symbol(self, symbol: str) -> None:
        async with self._lock:
            if self._symbol == symbol and self._task is not None and not self._task.done():
                return
            previous = self._task
            self._symbol = symbol
            self._price = None
            self._updated_at = 0.0
            self._ready.clear()
            self._task = asyncio.create_task(self._run(symbol), name=f"mexc-ticker-{symbol}")
        if previous is not None:
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)

    async def _run(self, symbol: str) -> None:
        backoff = 1.0
        while self._symbol == symbol:
            try:
                async with connect(
                    self.url,
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=None,
                    max_queue=32,
                ) as ws:
                    await ws.send(json.dumps({"method": "sub.ticker", "param": {"symbol": symbol}}))
                    LOGGER.info("MEXC ticker WebSocket subscribed symbol=%s", symbol)
                    backoff = 1.0
                    last_ping = time.monotonic()
                    while self._symbol == symbol:
                        try:
                            message: Any = await asyncio.wait_for(ws.recv(), timeout=15.0)
                        except TimeoutError:
                            await ws.send('{"method":"ping"}')
                            last_ping = time.monotonic()
                            continue
                        now = time.monotonic()
                        if now - last_ping >= 15.0:
                            await ws.send('{"method":"ping"}')
                            last_ping = now
                        price = ticker_price_from_message(message, symbol)
                        if price is None:
                            continue
                        self._price = price
                        self._updated_at = now
                        self._ready.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "MEXC ticker WebSocket disconnected symbol=%s error=%s; reconnecting in %.0fs",
                    symbol,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
