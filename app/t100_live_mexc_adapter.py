"""Concrete MEXC futures POST/GET adapter. POST sends real orders; nothing runs on import."""
from __future__ import annotations
from typing import Any
from app.mexc_trade import MexcTradeClient
from app.t100_live_authorization import (
    LiveMutationPermit,
    require_live_mutation_permit,
)
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

    async def order_deal_details(
        self,
        order_id: str,
    ) -> list[dict[str, Any]]:
        if (
            not isinstance(order_id, str)
            or not order_id.isdigit()
            or int(order_id) <= 0
        ):
            raise ValueError("invalid MEXC order id")
        data = await self.client._private_get(
            f"/api/v1/private/order/deal_details/{order_id}"
        )
        if not isinstance(data, list) or not all(
            isinstance(item, dict) for item in data
        ):
            raise RuntimeError("MEXC_ORDER_DEAL_DETAILS_UNVERIFIED")
        return data

    async def submit_short_with_stop(
        self,
        intent: EntryIntent,
        *,
        permit: LiveMutationPermit,
    ) -> str:
        permit = require_live_mutation_permit(permit)
        if intent.notional_usdt > permit.max_position_notional_usdt:
            raise RuntimeError("MEXC_ENTRY_EXCEEDS_PERMIT")
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
                "lossTrend": 1,
            },
        )
        if not isinstance(result, dict) or not result.get("orderId"):
            raise RuntimeError("MEXC did not return orderId")
        return str(result["orderId"])

    async def submit_native_short_stop_tightening(
        self,
        *,
        stop_order_id: int,
        new_stop_price: str,
        previous_stop_price: str,
        permit: LiveMutationPermit,
    ) -> None:
        """Send exactly one native TP/SL price amendment request.

        This transport method never retries and never interprets an exception
        as proof the exchange did not mutate. Durable callers must journal
        SUBMITTING before invoking it and reconcile UNKNOWN outcomes by GET.
        """
        from decimal import Decimal
        require_live_mutation_permit(permit)
        current = Decimal(previous_stop_price)
        requested = Decimal(new_stop_price)
        if (
            not current.is_finite()
            or not requested.is_finite()
            or current <= 0
            or requested <= 0
            or requested >= current
            or isinstance(stop_order_id, bool)
            or not isinstance(stop_order_id, int)
            or stop_order_id <= 0
        ):
            raise ValueError(
                "non-tightening or invalid short stop modification"
            )
        await self.client._private_post(
            "/api/v1/private/stoporder/change_plan_price",
            {
                "stopPlanOrderId": stop_order_id,
                "lossTrend": 1,
                "stopLossPrice": str(requested),
            },
        )

    async def tighten_native_short_stop(
        self, *, symbol: str, stop_order_id: int, position_id: int,
        new_stop_price: str, previous_stop_price: str,
        permit: LiveMutationPermit,
    ) -> None:
        """Legacy send-then-read helper; never retries ambiguous mutations."""
        from decimal import Decimal
        requested = Decimal(new_stop_price)
        await self.submit_native_short_stop_tightening(
            stop_order_id=stop_order_id,
            new_stop_price=new_stop_price,
            previous_stop_price=previous_stop_price,
            permit=permit,
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
        data = await self.client.open_stop_orders(symbol)
        if not isinstance(data, list) or not all(
            isinstance(item, dict) for item in data
        ):
            raise RuntimeError("MEXC_OPEN_STOP_RESPONSE_UNVERIFIED")
        return data
