from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

LOGGER = logging.getLogger(__name__)


def ticker_price_from_message(message: str | bytes, symbol: str) -> float | None:
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


@dataclass(slots=True)
class _TickerState:
    price: float | None = None
    updated_at: float = 0.0
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class MexcTickerStream:
    """Concurrent single-symbol streams, one lightweight WS connection per open symbol."""

    def __init__(self, url: str = "wss://contract.mexc.com/edge") -> None:
        self.url = url
        self._states: dict[str, _TickerState] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._lock:
            tasks = [s.task for s in self._states.values() if s.task is not None]
            self._states.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def remove(self, symbol: str) -> None:
        async with self._lock:
            state = self._states.pop(symbol, None)
        if state and state.task:
            state.task.cancel()
            await asyncio.gather(state.task, return_exceptions=True)

    async def last_price(
        self, symbol: str, *, timeout_seconds: float = 4.0, max_age_seconds: float = 5.0
    ) -> float:
        state = await self._ensure_symbol(symbol)
        if self._fresh(state, max_age_seconds):
            assert state.price is not None
            return state.price
        state.ready.clear()
        if self._fresh(state, max_age_seconds):
            assert state.price is not None
            return state.price
        try:
            await asyncio.wait_for(state.ready.wait(), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise RuntimeError(f"MEXC ticker WebSocket timed out for {symbol}") from exc
        if state.price is None:
            raise RuntimeError(f"MEXC ticker WebSocket has no price for {symbol}")
        return state.price

    @staticmethod
    def _fresh(state: _TickerState, max_age_seconds: float) -> bool:
        return state.price is not None and (time.monotonic() - state.updated_at) <= max_age_seconds

    async def _ensure_symbol(self, symbol: str) -> _TickerState:
        async with self._lock:
            state = self._states.get(symbol)
            if state is None:
                state = _TickerState()
                state.task = asyncio.create_task(self._run(symbol, state), name=f"mexc-ticker-{symbol}")
                self._states[symbol] = state
            elif state.task is None or state.task.done():
                state.task = asyncio.create_task(self._run(symbol, state), name=f"mexc-ticker-{symbol}")
            return state

    async def _run(self, symbol: str, state: _TickerState) -> None:
        backoff = 1.0
        while self._states.get(symbol) is state:
            try:
                try:
                    from websockets.asyncio.client import connect
                except ModuleNotFoundError as exc:
                    raise RuntimeError("Python package 'websockets' is not installed") from exc
                async with connect(
                    self.url, open_timeout=10, close_timeout=5, ping_interval=None, max_queue=32
                ) as ws:
                    await ws.send(json.dumps({"method": "sub.ticker", "param": {"symbol": symbol}}))
                    LOGGER.info("MEXC ticker WebSocket subscribed symbol=%s", symbol)
                    backoff = 1.0
                    last_ping = time.monotonic()
                    while self._states.get(symbol) is state:
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
                        state.price = price
                        state.updated_at = now
                        state.ready.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "MEXC ticker WebSocket disconnected symbol=%s error=%s; reconnecting in %.0fs",
                    symbol, exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
