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

    async def tighten_native_short_stop(self, *, symbol: str, stop_order_id: int,
                                        position_id: int, new_stop_price: str,
                                        previous_stop_price: str) -> None:
        """In-place TP/SL modification; never loosen a short protective stop.

        Ambiguous mutation or missing post-update verification MUST halt the
        caller, not trigger a blind retry.
        """
        from decimal import Decimal
        current, requested = Decimal(previous_stop_price), Decimal(new_stop_price)
        if not (current.is_finite() and requested.is_finite()
                and current > 0 and requested > 0 and requested < current):
            raise ValueError("non-tightening or invalid short stop modification")
        await self.client._private_post(
            "/api/v1/private/stoporder/change_plan_price",
            {"stopPlanOrderId": stop_order_id, "lossTrend": 2,
             "stopLossPrice": str(requested)},
        )
        orders = await self.open_stop_orders(symbol)
        ok = any(
            str(item.get("id")) == str(stop_order_id)
            and str(item.get("positionId")) == str(position_id)
            and int(item.get("state", 0)) == 1
            and Decimal(str(item.get("stopLossPrice") or 0)) <= requested
            for item in orders
        )
        if not ok:
            raise RuntimeError("NATIVE_STOP_MODIFICATION_UNVERIFIED")

    async def open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        # Do not interpret an unexpected/NULL account response as an empty account.
        data = await self.client._private_get(
            "/api/v1/private/position/open_positions", {"symbol": symbol})
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise RuntimeError("MEXC_OPEN_POSITION_RESPONSE_UNVERIFIED")
        return data

    async def open_stop_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return await self.client.stop_orders(symbol=symbol, finished=False)
