from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import time
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.mexc_price_stream import MexcTickerStream


LOGGER = logging.getLogger(__name__)


class MexcTradeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ContractSpec:
    symbol: str
    contract_size: float
    vol_unit: float
    min_vol: float
    max_vol: float
    api_allowed: bool
    position_open_type: int

    def contracts_for_notional(self, notional_usdt: float, price: float) -> float:
        raw = notional_usdt / (price * self.contract_size)
        units = math.floor(raw / self.vol_unit + 1e-12)
        vol = units * self.vol_unit
        if vol < self.min_vol:
            raise MexcTradeError(
                f"notional {notional_usdt:.2f} USDT is below minimum contract volume for {self.symbol}"
            )
        return min(vol, self.max_vol)


class MexcTradeClient:
    def __init__(self, base_url: str, api_key: str | None = None, api_secret: str | None = None) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.ticker_stream = MexcTickerStream()
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "mexc-standard-short-trader/1.1.3"},
        )

    async def close(self) -> None:
        await self.ticker_stream.close()
        await self.client.aclose()

    async def _public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self.client.get(path, params=params)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise MexcTradeError(f"MEXC error {payload.get('code')}: {payload.get('message')}")
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def _auth_headers(self, timestamp: str, parameter_string: str) -> dict[str, str]:
        if not self.api_key or not self.api_secret:
            raise MexcTradeError("MEXC API credentials are not configured")
        target = f"{self.api_key}{timestamp}{parameter_string}".encode()
        signature = hmac.new(self.api_secret.encode(), target, hashlib.sha256).hexdigest()
        return {
            "ApiKey": self.api_key,
            "Request-Time": timestamp,
            "Signature": signature,
            "Content-Type": "application/json",
        }

    async def _private_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        parameter_string = urlencode(sorted(params.items()))
        timestamp = str(int(time.time() * 1000))
        response = await self.client.get(
            path,
            params=params,
            headers=self._auth_headers(timestamp, parameter_string),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise MexcTradeError(
                f"MEXC private GET failed: {payload.get('code') if isinstance(payload, dict) else ''} "
                f"{payload.get('message') if isinstance(payload, dict) else payload}"
            )
        return payload.get("data")

    async def _private_post(self, path: str, body: dict[str, Any]) -> Any:
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        timestamp = str(int(time.time() * 1000))
        response = await self.client.post(
            path,
            content=raw,
            headers=self._auth_headers(timestamp, raw),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise MexcTradeError(
                f"MEXC private POST failed: {payload.get('code') if isinstance(payload, dict) else ''} "
                f"{payload.get('message') if isinstance(payload, dict) else payload}"
            )
        return payload.get("data")

    async def last_price(self, symbol: str) -> float:
        # MEXC recommends WebSocket for market trends. The trader keeps only one
        # position open, so one single-symbol stream replaces repetitive REST polls.
        try:
            return await self.ticker_stream.last_price(symbol)
        except Exception as exc:
            LOGGER.warning("Ticker WebSocket unavailable for %s (%s); using REST fallback", symbol, exc)
            return await self._rest_last_price(symbol)

    async def _rest_last_price(self, symbol: str) -> float:
        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                data = await self._public_get("/api/v1/contract/ticker", {"symbol": symbol})
                if isinstance(data, list):
                    data = next((row for row in data if str(row.get("symbol")) == symbol), None)
                if not isinstance(data, dict) or not data.get("lastPrice"):
                    raise MexcTradeError(f"No ticker for {symbol}")
                return float(data["lastPrice"])
            except MexcTradeError as exc:
                last_error = exc
                if "error 510" not in str(exc).lower() or attempt == 2:
                    raise
                LOGGER.warning(
                    "MEXC REST ticker rate-limited for %s; backing off %.0fs",
                    symbol,
                    delay,
                )
                await asyncio.sleep(delay)
                delay *= 2.0
        raise MexcTradeError(f"No ticker for {symbol}: {last_error}")

    async def contract_spec(self, symbol: str) -> ContractSpec:
        data = await self._public_get("/api/v1/contract/detail", {"symbol": symbol})
        rows = data if isinstance(data, list) else [data]
        row = next((r for r in rows if isinstance(r, dict) and str(r.get("symbol")) == symbol), None)
        if row is None:
            raise MexcTradeError(f"No contract spec for {symbol}")
        return ContractSpec(
            symbol=symbol,
            contract_size=float(row["contractSize"]),
            vol_unit=float(row.get("volUnit") or 1),
            min_vol=float(row.get("minVol") or 1),
            max_vol=float(row.get("maxVol") or 1e18),
            api_allowed=bool(row.get("apiAllowed", False)),
            position_open_type=int(row.get("positionOpenType") or 3),
        )

    async def usdt_equity(self) -> float:
        data = await self._private_get("/api/v1/private/account/asset/USDT")
        if not isinstance(data, dict):
            raise MexcTradeError("Unexpected USDT asset response")
        return float(data.get("equity") or 0.0)

    async def open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        data = await self._private_get("/api/v1/private/position/open_positions", params)
        return [row for row in (data or []) if isinstance(row, dict)]

    async def order(self, order_id: int) -> dict[str, Any]:
        data = await self._private_get(f"/api/v1/private/order/get/{order_id}")
        if not isinstance(data, dict):
            raise MexcTradeError(f"Unexpected order response for {order_id}")
        return data

    async def submit_market_short(
        self,
        *,
        symbol: str,
        contracts: float,
        open_type: int,
        reference_price: float,
        external_oid: str,
    ) -> int:
        body = {
            "symbol": symbol,
            "price": reference_price,
            "vol": contracts,
            "leverage": 1,
            "side": 3,
            "type": 5,
            "openType": open_type,
            "externalOid": external_oid,
        }
        order_id = await self._private_post("/api/v1/private/order/submit", body)
        return int(order_id)

    async def close_market_short(
        self,
        *,
        symbol: str,
        contracts: float,
        open_type: int,
        reference_price: float,
        position_id: int | None,
        external_oid: str,
    ) -> int:
        body: dict[str, Any] = {
            "symbol": symbol,
            "price": reference_price,
            "vol": contracts,
            "leverage": 1,
            "side": 2,
            "type": 5,
            "openType": open_type,
            "externalOid": external_oid,
        }
        if position_id is not None:
            body["positionId"] = position_id
        order_id = await self._private_post("/api/v1/private/order/submit", body)
        return int(order_id)
