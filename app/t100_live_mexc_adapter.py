"""Concrete MEXC futures POST/GET adapter. POST sends real orders; nothing runs on import."""
from __future__ import annotations
from typing import Any
from app.mexc_trade import MexcTradeClient
from app.t100_live_execution import EntryIntent

class LiveMexcAdapter:
    def __init__(self, client: MexcTradeClient) -> None:
        self.client = client

    async def order_by_external_oid(self, symbol: str, external_oid: str) -> dict[str, Any] | None:
        data = await self.client._private_get(f"/api/v1/private/order/external/{symbol}/{external_oid}")
        if data is None:
            return None
        if not isinstance(data, dict):
            raise RuntimeError("unexpected external order response")
        return data

    async def submit_short_with_stop(self, intent: EntryIntent) -> str:
        result = await self.client._private_post(
            "/api/v1/private/order/create",
            {
                "symbol": intent.symbol,
                "price": str(intent.reference_price),
                "vol": str(intent.volume_contracts),
                "leverage": 1,
                "side": 3,
                "type": 5,
                "openType": 1,
                "positionMode": 1,
                "externalOid": intent.external_oid,
                "stopLossPrice": str(intent.stop_price),
                "lossTrend": 2,
            },
        )
        if not isinstance(result, dict) or not result.get("orderId"):
            raise RuntimeError("MEXC did not return orderId")
        return str(result["orderId"])

    async def open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return await self.client.open_positions(symbol)

    async def open_stop_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return await self.client.stop_orders(symbol=symbol, finished=False)
