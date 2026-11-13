"""Unreleased V40 Discord business-event formatting and exact deduplication keys.

Every event requires the corresponding independent evidence classification.
A formatter does NOT independently verify MEXC or write orders: verified
exchange/DB readers must provide those facts before an event is constructed.
No TP1/TP2 orders exist in frozen V40. +5% is informational only.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
from hashlib import sha256
import json
import re
from typing import Any

_KINDS: dict[str, tuple[str, str, int]] = {
    "STAGE2_QUALIFIED": ("🟡 V40 SIGNAL QUALIFIED", "V40_STAGE2_PARITY_PASS", 0xFEE75C),
    "ENTRY_FILLED": ("🟢 SHORT POSITION OPENED", "MEXC_ENTRY_FILLS_VERIFIED", 0x57F287),
    "NATIVE_SL_ACTIVE": ("🔴 SL75 PROTECTION ACTIVE — NOT TRIGGERED", "MEXC_NATIVE_SL_GET_VERIFIED", 0xED4245),
    "PROFIT_5_OBSERVED": ("🔵 +5% FAVORABLE MOVE — NOT A TAKE PROFIT", "MEXC_MARKET_PRICE_VERIFIED", 0x00B0F4),
    "TRAIL_ARMED": ("🟣 +14% TRAILING ACTIVATED", "MEXC_TRAIL_STOP_GET_VERIFIED", 0x9B59B6),
    "TRAIL_TIGHTENED": ("🟣 NATIVE TRAILING STOP TIGHTENED", "MEXC_TRAIL_STOP_GET_VERIFIED", 0x9B59B6),
    "SL_EXECUTED": ("🛑 STOP LOSS EXECUTED", "MEXC_FINISHED_SL_AND_FILLS_VERIFIED", 0x992D22),
    "EXIT_PROFIT": ("💰 POSITION CLOSED — PROFIT", "MEXC_CLOSED_POSITION_AND_PNL_VERIFIED", 0x1F8B4C),
    "EXIT_NONPROFIT": ("📊 POSITION CLOSED — NONPROFIT", "MEXC_CLOSED_POSITION_AND_PNL_VERIFIED", 0x747F8D),
    "EMERGENCY_CLOSE": ("🟠 EMERGENCY CLOSE CONFIRMED", "MEXC_EMERGENCY_CLOSE_AND_FLAT_VERIFIED", 0xE67E22),
    "ACTION_REQUIRED": ("⚠️ V40 MANUAL RECONCILIATION REQUIRED", "V40_CRITICAL_INCIDENT_DURABLE", 0xF39C12),
}
_SYMBOL = re.compile(r"[A-Z0-9]{2,24}_USDT\Z")
_MARKER = re.compile(r"[A-Za-z0-9:_-]{1,128}\Z")


class V40EventInvalid(ValueError):
    """No raw event payloads, webhook tokens, or API secrets."""


@dataclass(frozen=True)
class V40BusinessEvent:
    kind: str
    symbol: str
    signal_id: int
    evidence_status: str
    evidence_id: str
    observed_at_utc: str
    position_id: str | None = None
    price: D | None = None
    notional_usdt: D | None = None
    realized_pnl_usdt: D | None = None

    def __post_init__(self) -> None:
        expected = _KINDS.get(self.kind)
        if (
            expected is None
            or self.evidence_status != expected[1]
            or type(self.symbol) is not str
            or _SYMBOL.fullmatch(self.symbol) is None
            or type(self.signal_id) is not int
            or self.signal_id <= 0
            or type(self.evidence_id) is not str
            or _MARKER.fullmatch(self.evidence_id) is None
            or type(self.observed_at_utc) is not str
            or len(self.observed_at_utc) < 19
            or not self.observed_at_utc.endswith("Z")
            or (self.position_id is not None and (
                type(self.position_id) is not str
                or not self.position_id.isascii()
                or not self.position_id.isdecimal()
                or self.position_id.startswith("0")
            ))
        ):
            raise V40EventInvalid("V40_BUSINESS_EVENT_PROVENANCE_INVALID")
        for field in (self.price, self.notional_usdt, self.realized_pnl_usdt):
            if field is not None and (
                type(field) is not D or not field.is_finite()
            ):
                raise V40EventInvalid("V40_BUSINESS_EVENT_AMOUNT_INVALID")
        if (
            self.price is not None and self.price <= 0
            or self.notional_usdt is not None and self.notional_usdt <= 0
            or self.kind in ("EXIT_PROFIT", "EXIT_NONPROFIT")
            and (
                self.position_id is None
                or self.realized_pnl_usdt is None
                or (self.kind == "EXIT_PROFIT" and self.realized_pnl_usdt <= 0)
                or (self.kind == "EXIT_NONPROFIT" and self.realized_pnl_usdt > 0)
            )
            or self.kind in (
                "ENTRY_FILLED", "NATIVE_SL_ACTIVE", "TRAIL_ARMED",
                "TRAIL_TIGHTENED", "SL_EXECUTED", "EMERGENCY_CLOSE",
            ) and self.position_id is None
        ):
            raise V40EventInvalid("V40_BUSINESS_EVENT_SEMANTICS_INVALID")

    @property
    def idempotency_key(self) -> str:
        # Event reference MUST identify the exchange fill, native stop,
        # completed bar or durable incident. Never use current wall-clock
        # time as the sole deduplication identity.
        origin = "|".join((
            "V40", self.kind, self.symbol, str(self.signal_id),
            self.position_id or "-", self.evidence_id,
        ))
        return sha256(origin.encode("utf-8")).hexdigest()

    def discord_embed(self) -> dict[str, Any]:
        title, _, color = _KINDS[self.kind]
        fields = [
            {"name": "Symbol", "value": self.symbol, "inline": True},
            {"name": "Signal ID", "value": str(self.signal_id), "inline": True},
            {"name": "Proof", "value": self.evidence_status, "inline": False},
            {"name": "Event reference", "value": self.evidence_id, "inline": False},
        ]
        if self.position_id:
            fields.append({"name": "Position", "value": self.position_id, "inline": True})
        for label, value in (
            ("Verified price", self.price),
            ("Position notional (USDT)", self.notional_usdt),
            ("Realized P&L (USDT)", self.realized_pnl_usdt),
        ):
            if value is not None:
                fields.append({"name": label, "value": str(value), "inline": True})
        fields.append({"name": "Observed at (UTC)", "value": self.observed_at_utc, "inline": False})
        return {
            "title": title, "color": color,
            "description": (
                "V40 native trade lifecycle. Confirmed facts only; "
                "a price milestone is not an executed take-profit."
            ),
            "fields": fields, "footer": {"text": "V40 event " + self.idempotency_key[:16]},
        }

    def durable_json(self) -> str:
        return json.dumps(self.discord_embed(), sort_keys=True, separators=(",", ":"))


class V40TradeOutbox:
    """A transaction-backed pending Discord outbox; no venue methods.

    Exactly-once insertion key, *at-least-once* webhook delivery. A timeout
    after Discord accepts a POST but before DB ACK may cause duplicate alerts.
    Requires the unreleased 041 migration, a single locked delivery worker,
    and independently reviewed DB grants before production use.
    """

    def __init__(self, conn: Any):
        if not all(callable(getattr(conn, attr, None)) for attr in (
            "fetchrow", "fetch", "fetchval", "execute"
        )):
            raise V40EventInvalid("V40_TRADE_OUTBOX_CONNECTION_INVALID")
        self._conn = conn

    async def enqueue(self, event: V40BusinessEvent) -> bool:
        if type(event) is not V40BusinessEvent:
            raise V40EventInvalid("V40_TRADE_OUTBOX_EVENT_INVALID")
        try:
            row = await self._conn.fetchrow(
                """INSERT INTO public.t100_v40_trade_event_outbox
                   (event_key, event_kind, payload)
                   VALUES ($1,$2,$3::jsonb)
                   ON CONFLICT (event_key) DO UPDATE SET event_key=EXCLUDED.event_key
                   RETURNING event_kind, payload""",
                event.idempotency_key, event.kind, event.durable_json(),
            )
        except Exception:
            raise V40EventInvalid("V40_TRADE_OUTBOX_WRITE_FAILED") from None
        if row is None or row["event_kind"] != event.kind or json.dumps(
            row["payload"], sort_keys=True, separators=(",", ":")
        ) != event.durable_json():
            raise V40EventInvalid("V40_TRADE_OUTBOX_IDEMPOTENCY_COLLISION")
        return True

    async def pending(self, limit: int = 10) -> tuple[tuple[str, dict[str, Any]], ...]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise V40EventInvalid("V40_TRADE_OUTBOX_LIMIT_INVALID")
        try:
            rows = await self._conn.fetch(
                """SELECT event_key, payload
                   FROM public.t100_v40_trade_event_outbox
                   WHERE delivered_at IS NULL
                   ORDER BY created_at,event_key LIMIT $1""", limit
            )
        except Exception:
            raise V40EventInvalid("V40_TRADE_OUTBOX_READ_FAILED") from None
        if type(rows) not in (list, tuple) or len(rows) > limit:
            raise V40EventInvalid("V40_TRADE_OUTBOX_READ_UNVERIFIED")
        for row in rows:
            if (
                type(row["event_key"]) is not str
                or not re.fullmatch(r"[a-f0-9]{64}", row["event_key"])
                or type(row["payload"]) is not dict
            ):
                raise V40EventInvalid("V40_TRADE_OUTBOX_PAYLOAD_INVALID")
        return tuple((r["event_key"], r["payload"]) for r in rows)

    async def mark_delivered(self, event_key: str) -> None:
        if type(event_key) is not str or re.fullmatch(r"[a-f0-9]{64}", event_key) is None:
            raise V40EventInvalid("V40_TRADE_OUTBOX_KEY_INVALID")
        try:
            result = await self._conn.fetchval(
                """UPDATE public.t100_v40_trade_event_outbox
                   SET delivered_at=clock_timestamp(),attempts=attempts+1
                   WHERE event_key=$1 AND delivered_at IS NULL
                   RETURNING event_key""", event_key,
            )
        except Exception:
            raise V40EventInvalid("V40_TRADE_OUTBOX_ACK_FAILED") from None
        if result != event_key:
            raise V40EventInvalid("V40_TRADE_OUTBOX_ACK_UNVERIFIED")
