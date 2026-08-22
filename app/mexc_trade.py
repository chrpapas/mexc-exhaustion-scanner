from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import time
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
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        ws_url: str = "wss://contract.mexc.com/edge",
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.ticker_stream = MexcTickerStream(ws_url)
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "mexc-exhaustion-multislot-trader/1.2.0", "Language": "en-US"},
        )
        self._spec_cache: dict[str, ContractSpec] = {}
        self._mutation_lock = asyncio.Lock()
        self._last_mutation_at = 0.0

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
            "Recv-Window": "10",
            "Content-Type": "application/json",
        }

    async def _private_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        parameter_string = urlencode(sorted(params.items()))
        timestamp = str(int(time.time() * 1000))
        response = await self.client.get(
            path, params=params, headers=self._auth_headers(timestamp, parameter_string)
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise MexcTradeError(
                f"MEXC private GET failed: {payload.get('code') if isinstance(payload, dict) else ''} "
                f"{payload.get('message') if isinstance(payload, dict) else payload}"
            )
        return payload.get("data")

    async def _private_post(self, path: str, body: dict[str, Any], *, throttle: bool = True) -> Any:
        body = {k: v for k, v in body.items() if v is not None}
        if throttle:
            async with self._mutation_lock:
                wait = 0.55 - (time.monotonic() - self._last_mutation_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                result = await self._private_post(path, body, throttle=False)
                self._last_mutation_at = time.monotonic()
                return result
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        timestamp = str(int(time.time() * 1000))
        response = await self.client.post(
            path, content=raw, headers=self._auth_headers(timestamp, raw)
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise MexcTradeError(
                f"MEXC private POST failed: {payload.get('code') if isinstance(payload, dict) else ''} "
                f"{payload.get('message') if isinstance(payload, dict) else payload}"
            )
        return payload.get("data")

    async def ping(self) -> int:
        data = await self._public_get("/api/v1/contract/ping")
        return int(data)

    async def last_price(self, symbol: str) -> float:
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
                if "510" not in str(exc) or attempt == 2:
                    raise
                await asyncio.sleep(delay)
                delay *= 2.0
        raise MexcTradeError(f"No ticker for {symbol}: {last_error}")

    async def refresh_contract_specs(self) -> None:
        data = await self._public_get("/api/v1/contract/detail")
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if not isinstance(row, dict) or not row.get("symbol") or not row.get("contractSize"):
                continue
            symbol = str(row["symbol"])
            self._spec_cache[symbol] = ContractSpec(
                symbol=symbol,
                contract_size=float(row["contractSize"]),
                vol_unit=float(row.get("volUnit") or 1),
                min_vol=float(row.get("minVol") or 1),
                max_vol=float(row.get("maxVol") or 1e18),
                api_allowed=bool(row.get("apiAllowed", False)),
                position_open_type=int(row.get("positionOpenType") or 3),
            )

    async def contract_spec(self, symbol: str) -> ContractSpec:
        cached = self._spec_cache.get(symbol)
        if cached is not None:
            return cached
        data = await self._public_get("/api/v1/contract/detail", {"symbol": symbol})
        rows = data if isinstance(data, list) else [data]
        row = next((r for r in rows if isinstance(r, dict) and str(r.get("symbol")) == symbol), None)
        if row is None:
            raise MexcTradeError(f"No contract spec for {symbol}")
        spec = ContractSpec(
            symbol=symbol,
            contract_size=float(row["contractSize"]),
            vol_unit=float(row.get("volUnit") or 1),
            min_vol=float(row.get("minVol") or 1),
            max_vol=float(row.get("maxVol") or 1e18),
            api_allowed=bool(row.get("apiAllowed", False)),
            position_open_type=int(row.get("positionOpenType") or 3),
        )
        self._spec_cache[symbol] = spec
        return spec

    async def usdt_asset(self) -> dict[str, Any]:
        data = await self._private_get("/api/v1/private/account/asset/USDT")
        if not isinstance(data, dict):
            raise MexcTradeError("Unexpected USDT asset response")
        return data

    async def usdt_equity(self) -> float:
        return float((await self.usdt_asset()).get("equity") or 0.0)

    async def usdt_available_balance(self) -> float:
        """Return spendable USDT in the MEXC futures account.

        MEXC currently exposes ``availableBalance`` on the contract asset endpoint.
        The fallbacks cover older/variant payload names without guessing from equity.
        """
        asset = await self.usdt_asset()
        for key in ("availableBalance", "availableCash", "available"): 
            value = asset.get(key)
            if value is None or value == "":
                continue
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                continue
        # If no explicit availability field is present, fail closed rather than sizing
        # live orders from total equity that may already be committed as margin.
        raise MexcTradeError("MEXC USDT asset response has no usable available-balance field")

    async def open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        data = await self._private_get("/api/v1/private/position/open_positions", {"symbol": symbol})
        return [row for row in (data or []) if isinstance(row, dict)]

    async def position_mode(self) -> int | None:
        data = await self._private_get("/api/v1/private/position/position_mode")
        if isinstance(data, dict):
            data = data.get("positionMode") or data.get("mode")
        if isinstance(data, (int, float, str)) and str(data).strip():
            try:
                return int(data)
            except (TypeError, ValueError):
                return None
        # Some current MEXC documentation examples for this endpoint are inconsistent;
        # an unparseable response is treated as unknown rather than guessed.
        return None

    async def order(self, order_id: int) -> dict[str, Any]:
        data = await self._private_get(f"/api/v1/private/order/get/{order_id}")
        if not isinstance(data, dict):
            raise MexcTradeError(f"Unexpected order response for {order_id}")
        return data

    async def submit_market_short(
        self, *, symbol: str, contracts: float, open_type: int, reference_price: float,
        external_oid: str, leverage: int = 1,
    ) -> int:
        data = await self._private_post(
            "/api/v1/private/order/create",
            {
                "symbol": symbol,
                "price": reference_price,
                "vol": contracts,
                "leverage": leverage,
                "side": 3,
                "type": 5,
                "openType": open_type,
                "externalOid": external_oid,
                "positionMode": 1,
            },
        )
        order_id = data.get("orderId") if isinstance(data, dict) else data
        return int(order_id)

    async def close_market_short(
        self, *, symbol: str, contracts: float, open_type: int, reference_price: float,
        position_id: int | None, external_oid: str, leverage: int = 1,
    ) -> int:
        data = await self._private_post(
            "/api/v1/private/order/create",
            {
                "symbol": symbol,
                "price": reference_price,
                "vol": contracts,
                "leverage": leverage,
                "side": 2,
                "type": 5,
                "openType": open_type,
                "positionId": position_id,
                "externalOid": external_oid,
                "positionMode": 1,
            },
        )
        order_id = data.get("orderId") if isinstance(data, dict) else data
        return int(order_id)

    async def place_position_stop(
        self, *, position_id: int, contracts: float, stop_price: float
    ) -> int:
        data = await self._private_post(
            "/api/v1/private/stoporder/place",
            {
                "lossTrend": 1,
                "profitTrend": 1,
                "positionId": position_id,
                "vol": contracts,
                "stopLossPrice": stop_price,
                "priceProtect": 0,
                "profitLossVolType": "SAME",
                "volType": 2,
                "stopLossReverse": 2,
                "stopLossType": 0,
            },
        )
        if isinstance(data, list) and data:
            data = data[0].get("id") if isinstance(data[0], dict) else data[0]
        if isinstance(data, dict):
            data = data.get("id") or data.get("stopPlanOrderId")
        return int(data)

    async def modify_position_stop(self, *, stop_order_id: int, stop_price: float) -> None:
        await self._private_post(
            "/api/v1/private/stoporder/change_plan_price",
            {"stopPlanOrderId": stop_order_id, "lossTrend": 1, "stopLossPrice": stop_price},
        )

    async def cancel_position_stop(self, stop_order_id: int) -> None:
        await self._private_post(
            "/api/v1/private/stoporder/cancel",
            {"orders": [{"stopPlanOrderId": stop_order_id}]},
        )

    async def open_stop_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        data = await self._private_get(
            "/api/v1/private/stoporder/open_orders", {"symbol": symbol}
        )
        return [row for row in (data or []) if isinstance(row, dict)]

    async def stop_orders(self, *, symbol: str, finished: bool | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"symbol": symbol, "page_num": 1, "page_size": 100}
        if finished is not None:
            params["is_finished"] = 1 if finished else 0
        data = await self._private_get("/api/v1/private/stoporder/list/orders", params)
        if isinstance(data, dict):
            data = data.get("resultList") or data.get("rows") or []
        return [row for row in (data or []) if isinstance(row, dict)]
