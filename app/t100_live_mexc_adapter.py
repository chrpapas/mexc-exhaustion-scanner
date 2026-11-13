"""Concrete MEXC futures POST/GET adapter. POST sends real orders; import is inert.

Order/create payload contains only documented MEXC fields, matching the
successful supervised one-shot pilot's request shape. Unlike that isolated
1x pilot, V40 openType=2 remains CROSS and is NOT venue-certified. The
typed live permit and durable one-shot journal requirements remain intact.
"""
from __future__ import annotations
from decimal import Decimal as D
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.mexc_trade import MexcTradeClient
from app.t100_live_authorization import (
    LiveMutationPermit,
    require_certified_transport_permit,
)
from app.t100_live_execution import EntryIntent, deterministic_t100_entry_oid
from app.t100_v40_guardian_post_gate import (
    PostgresV40GuardianPostGate,V40GuardianPostGateEvidence,
)

class LiveMexcAdapter:
    # Pinned MEXC futures openType=2 (CROSS) for the V40 tiny pilot.
    # This is only a transport contract; it does not certify shared
    # collateral, account completeness, native-stop atomicity or allow POST.
    open_type = 2

    def __init__(
        self, client: MexcTradeClient,
        *, guardian_gate: PostgresV40GuardianPostGate|None=None,
    ) -> None:
        self.client = client
        self.guardian_gate = guardian_gate

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
        # The final send boundary must fail BEFORE any network call on a
        # malformed immutable V40 origin, non-finite amount or missing SL75.
        # This is defense in depth: the fenced coordinator also validates
        # the tick-rounded stop, journal state, full risk and lease.
        if type(intent) is not EntryIntent:
            raise RuntimeError("MEXC_ENTRY_INTENT_TYPE_INVALID_NO_POST")
        permit = require_certified_transport_permit(permit)
        from app.t100_live_authorization import V40_CANARY_SCOPE
        if getattr(permit,'scope',None)==V40_CANARY_SCOPE:
            values=(
                intent.reference_price,intent.notional_usdt,
                intent.volume_contracts,intent.stop_price,
            )
            if (
                type(intent.signal_id) is not int
                or intent.signal_id<=0
                or type(intent.symbol) is not str
                or re.fullmatch(r"[A-Z0-9]+_USDT",intent.symbol) is None
                or intent.symbol in ("BTC_USDT","ETH_USDT")
                or permit.signal_id!=intent.signal_id
                or permit.symbol!=intent.symbol
                or type(intent.external_oid) is not str
                or intent.external_oid!=deterministic_t100_entry_oid(
                    intent.signal_id,intent.symbol,
                )
                or any(
                    type(value) is not D
                    or not value.is_finite()
                    or value<=0 for value in values
                )
                or intent.stop_price<
                    intent.reference_price*D("1.75")
                or intent.notional_usdt>D("12.50")
                or intent.notional_usdt>permit.max_position_notional_usdt
                or self.open_type!=2
                or permit.leverage!=1
            ):
                raise RuntimeError("V40_CANARY_FROZEN_ORDER_OR_SL75_INVALID_NO_POST")
        if intent.notional_usdt > permit.max_position_notional_usdt:
            raise RuntimeError("MEXC_ENTRY_EXCEEDS_PERMIT")
        if getattr(permit,"scope",None)==V40_CANARY_SCOPE:
            # A valid permit can become unsafe milliseconds later. Every
            # *actual* V40 POST therefore requires a new restricted SQL
            # read of the independent guardian (no cached readiness flags).
            # Any missing gate, stale heartbeat, new critical incident or
            # unacknowledged alert fails BEFORE the venue request.
            if type(self.guardian_gate) is not PostgresV40GuardianPostGate:
                raise RuntimeError("V40_CANARY_GUARDIAN_GATE_MISSING_NO_POST")
            gate=await self.guardian_gate.verify()
            if (
                type(gate) is not V40GuardianPostGateEvidence
                or gate.status!=(
                    "V40_GUARDIAN_POST_GATE_CURRENT_FLAT_UNLATCHED_NONAUTHORIZING"
                )
                or gate.state_verified is not True
                or gate.outbox_verified is not True
                or gate.guardian_fresh is not True
                or gate.critical_latch_absent is not True
                or gate.entry_allowed is not False
                or gate.live_authorization is not False
                or gate.mexc_posts!=0
                or gate.database_writes!=0
            ):
                raise RuntimeError("V40_CANARY_GUARDIAN_LATCHED_OR_STALE_NO_POST")
        result = await self.client._private_post(
            "/api/v1/private/order/create",
            {
                "symbol": intent.symbol,
                "price": str(intent.reference_price),
                "vol": str(intent.volume_contracts),
                "leverage": 1,
                "side": 3,
                "type": 5,
                "openType": self.open_type,
                "positionMode": 1,
                "externalOid": intent.external_oid,
                "stopLossPrice": str(intent.stop_price),
                "lossTrend": 1,
            },
        )
        # The current MEXC order/create response returns a numeric string
        # orderId. Never journal a boolean, signed, zero, Unicode or
        # nonintegral value as a successful exchange acknowledgement.
        # MexcTradeClient._private_post returns response["data"], not the\n        # entire envelope. Current MEXC Futures order/create returns the\n        # ID as a scalar string; the older dict form is also supported.\n        # Any other shape is UNKNOWN after POST, never safe to retry.\n        raw_id = result.get("orderId") if type(result) is dict else result
        if (
            type(raw_id) not in (int, str)
            or re.fullmatch(r"[1-9][0-9]*", str(raw_id)) is None
        ):
            # The POST may already have succeeded. The durable caller must
            # reconcile by externalOid and MUST NOT retry this entry.
            raise RuntimeError("MEXC did not return orderId")
        return str(raw_id)

    async def submit_native_short_stop_tightening(
        self,
        *,
        stop_order_id: int,
        new_stop_price: str,
        previous_stop_price: str,
        permit: LiveMutationPermit,
    ) -> None:
        """Send exactly one native TP/SL price amendment request.

        The explicit stopLossReverse=2 matches the one supervised
        isolated-margin MEXC price edit observed in pilot 4 (no reversal).
        It is NOT independent proof for the V40 cross-margin adapter.
        This transport method never retries and never interprets an exception
        as proof the exchange did not mutate. Durable callers must journal
        SUBMITTING before invoking it and reconcile UNKNOWN outcomes by GET.
        """
        from decimal import Decimal
        require_certified_transport_permit(permit)
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
        from app import t100_live_guards
        from app.t100_live_authorization import LiveAuthorizationHalted
        if (
            t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS
            != "CERTIFIED"
        ):
            raise LiveAuthorizationHalted(
                "MEXC_NATIVE_STOP_AMENDMENT_CONTRACT_UNVERIFIED"
            )
        await self.client._private_post(
            "/api/v1/private/stoporder/change_plan_price",
            {
                "stopPlanOrderId": stop_order_id,
                "lossTrend": 1,
                "stopLossReverse": 2,
                "stopLossPrice": str(requested),
            },
        )

    async def tighten_native_short_stop(
        self, *, symbol: str, stop_order_id: int, position_id: int,
        new_stop_price: str, previous_stop_price: str,
        permit: LiveMutationPermit,
    ) -> None:
        """DISABLED: unsafe legacy send-then-read stop mutation bypass.

        This helper has no durable PREPARED -> SUBMITTING CAS and can never
        independently prove that one worker owns the exchange POST attempt.
        Calling it could duplicate a stop edit after a restart or ambiguity.
        Only StopMutationExecutor.submit_once may use the separate
        submit_native_short_stop_tightening transport *after* durable journal
        ownership; it still requires a certified permit and independent GET.
        """
        raise RuntimeError(
            "LEGACY_STOP_MUTATION_REQUIRES_DURABLE_JOURNAL"
        )

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
