"""Narrow MEXC emergency CLOSE_SHORT transport for the dedicated T100 pilot.

Not enabled by the Render worker or the paper trader.
It can submit at most one *call* per invocation, only the exact one-contract
BTC_USDT isolated-short close order shape proven in the V4 exchange drill.
The durable guardian must claim its immutable ledger intent before invoking.

Never opens, amends, cancels or selects a position. Credentials are supplied
to an injected, separately scoped client; never handled by this module.
The injected client must use the documented signed POST method without retry.
"""
from __future__ import annotations

from decimal import Decimal
import re
from typing import Any

from app.t100_live_owned_emergency_guardian import (
    OwnedShort, OwnedGuardianHalted, _validate_owned,
    close_intent_oid,
)


class PilotCloseOnlyMexcTransport:
    def __init__(self, client: Any, *, owned: OwnedShort,
                 account_scope_sha256: str,
                 emergency_close_enabled: bool = False):
        _validate_owned(owned, account_scope_sha256)
        if not (type(emergency_close_enabled) is bool and
                owned.symbol == "BTC_USDT" and
                owned.contracts == Decimal("1")):
            raise OwnedGuardianHalted("PILOT_CLOSE_SCOPE_OR_SIZE_UNVERIFIED")
        self.client = client
        self.owned = owned
        self.enabled = emergency_close_enabled

    async def close_short_once(self, *, external_oid: str,
                               symbol: str, position_id: str,
                               contracts: Decimal) -> Any:
        if self.enabled is not True:
            raise OwnedGuardianHalted("PILOT_EMERGENCY_CLOSE_DISABLED")
        if (external_oid != close_intent_oid(self.owned) or
                symbol != "BTC_USDT" or symbol != self.owned.symbol or
                position_id != self.owned.position_id or
                not re.fullmatch("[1-9][0-9]*", position_id or "") or
                type(contracts) is not Decimal or
                contracts != self.owned.contracts or contracts != 1):
            raise OwnedGuardianHalted("PILOT_CLOSE_ATTEMPT_WRONG_OWNED_POSITION")
        body = {
            "symbol": "BTC_USDT",
            "price": "0",
            "vol": 1,
            "side": 2,               # CLOSE_SHORT
            "sideType": "buy",
            "positionType": "short",
            "type": 5,               # MARKET
            "openType": 1,           # ISOLATED
            "positionId": int(position_id),
            "positionMode": 1,       # HEDGE
            "externalOid": external_oid,
        }
        # No retries, no catch-and-rePOST. The guardian journals SUBMITTING
        # before invoking and treats any uncertain outcome as UNKNOWN.
        ack = await self.client._private_post(
            "/api/v1/private/order/create", body,
        )
        if ack is None or ack is False:
            raise OwnedGuardianHalted("PILOT_CLOSE_ACK_AMBIGUOUS_RECONCILE_ONLY")
        # A nonempty acknowledgement is NOT a verified fill/flat account.
        return ack
