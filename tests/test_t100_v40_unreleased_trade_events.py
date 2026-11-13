"""Unreleased V40 trade-event regression tests: no Discord network calls."""
from decimal import Decimal as D

import pytest

from app.t100_v40_unreleased_trade_events import (
    V40BusinessEvent, V40EventInvalid, V40TradeOutbox,
)


def verified(kind="STAGE2_QUALIFIED", **kwargs):
    proofs = {
        "STAGE2_QUALIFIED": "V40_STAGE2_PARITY_PASS",
        "ENTRY_FILLED": "MEXC_ENTRY_FILLS_VERIFIED",
        "NATIVE_SL_ACTIVE": "MEXC_NATIVE_SL_GET_VERIFIED",
        "PROFIT_5_OBSERVED": "MEXC_MARKET_PRICE_VERIFIED",
        "TRAIL_ARMED": "MEXC_TRAIL_STOP_GET_VERIFIED",
        "TRAIL_TIGHTENED": "MEXC_TRAIL_STOP_GET_VERIFIED",
        "SL_EXECUTED": "MEXC_FINISHED_SL_AND_FILLS_VERIFIED",
        "EXIT_PROFIT": "MEXC_CLOSED_POSITION_AND_PNL_VERIFIED",
        "EXIT_NONPROFIT": "MEXC_CLOSED_POSITION_AND_PNL_VERIFIED",
        "EMERGENCY_CLOSE": "MEXC_EMERGENCY_CLOSE_AND_FLAT_VERIFIED",
        "ACTION_REQUIRED": "V40_CRITICAL_INCIDENT_DURABLE",
    }
    base = {
        "kind": kind, "symbol": "EXAMPLE_USDT", "signal_id": 496,
        "evidence_id": "filled:123", "evidence_status": proofs[kind],
        "observed_at_utc": "2026-11-13T13:00:00Z",
    }
    if kind not in {"STAGE2_QUALIFIED", "PROFIT_5_OBSERVED", "ACTION_REQUIRED"}:
        base["position_id"] = "555"
    if kind in {"EXIT_PROFIT", "EXIT_NONPROFIT"}:
        base["realized_pnl_usdt"] = D("1.25") if kind == "EXIT_PROFIT" else D("-0.25")
    base.update(kwargs)
    return V40BusinessEvent(**base)


def test_color_scheme_matches_live_semantics_not_trade_intent():
    for kind, color in (
        ("STAGE2_QUALIFIED", 0xFEE75C), ("ENTRY_FILLED", 0x57F287),
        ("NATIVE_SL_ACTIVE", 0xED4245), ("PROFIT_5_OBSERVED", 0x00B0F4),
        ("TRAIL_ARMED", 0x9B59B6), ("SL_EXECUTED", 0x992D22),
        ("EXIT_PROFIT", 0x1F8B4C), ("EXIT_NONPROFIT", 0x747F8D),
        ("ACTION_REQUIRED", 0xF39C12),
    ):
        assert verified(kind).discord_embed()["color"] == color


def test_native_stop_active_is_not_a_triggered_sl():
    assert "NOT TRIGGERED" in verified("NATIVE_SL_ACTIVE").discord_embed()["title"]
    assert "STOP LOSS EXECUTED" in verified("SL_EXECUTED").discord_embed()["title"]


def test_plus_five_is_informational_only_not_take_profit():
    title = verified("PROFIT_5_OBSERVED").discord_embed()["title"]
    assert "NOT A TAKE PROFIT" in title


def test_notification_id_is_deterministic_across_restarts():
    a = verified("ENTRY_FILLED")
    assert a.idempotency_key == verified("ENTRY_FILLED").idempotency_key
    assert a.idempotency_key != verified(
        "ENTRY_FILLED", evidence_id="filled:other"
    ).idempotency_key
    assert len(a.idempotency_key) == 64


def test_no_false_claims_or_unverified_profit():
    with pytest.raises(V40EventInvalid, match="PROVENANCE"):
        verified("ENTRY_FILLED", evidence_status="EXCHANGE_POST_ACK_ONLY")
    with pytest.raises(V40EventInvalid, match="SEMANTICS"):
        verified("EXIT_PROFIT", realized_pnl_usdt=D("-0.01"))
    with pytest.raises(V40EventInvalid, match="SEMANTICS"):
        verified("ENTRY_FILLED", position_id=None)
    with pytest.raises(V40EventInvalid, match="AMOUNT"):
        verified("ENTRY_FILLED", price=D("NaN"))
    with pytest.raises(V40EventInvalid, match="SEMANTICS"):
        verified("ENTRY_FILLED", notional_usdt=D("0"))


def test_rendered_embed_contains_only_provided_verified_numbers():
    event = verified(
        "ENTRY_FILLED", price=D("123.45"), notional_usdt=D("12.50")
    )
    labels = {f["name"]: f["value"] for f in event.discord_embed()["fields"]}
    assert labels["Verified price"] == "123.45"
    assert labels["Position notional (USDT)"] == "12.50"
    assert "Realized P&L (USDT)" not in labels


class FakeConn:
    def __init__(self):
        self.records = {}

    async def fetchrow(self, statement, *args):
        if statement.startswith("INSERT"):
            key, kind, payload = args
            if key in self.records:
                return None
            import json
            self.records[key] = {"event_kind": kind, "payload": json.loads(payload)}
            return self.records[key]
        key = args[0]
        return self.records.get(key)

    async def fetch(self, statement, limit):
        return [
            {"event_key": k, "payload": v["payload"]}
            for k, v in list(self.records.items())[:limit]
        ]

    async def fetchval(self, statement, key):
        assert key in self.records
        return key

    async def execute(self, statement, *args):
        return "OK"


@pytest.mark.asyncio
async def test_outbox_durable_identity_no_duplicate_entries():
    fake = FakeConn()
    outbox = V40TradeOutbox(fake)
    event = verified("ENTRY_FILLED")
    assert await outbox.enqueue(event) is True
    assert await outbox.enqueue(event) is True
    assert len(fake.records) == 1
    pending = await outbox.pending()
    assert len(pending) == 1
    assert pending[0][0] == event.idempotency_key
    await outbox.mark_delivered(event.idempotency_key)


@pytest.mark.asyncio
async def test_outbox_key_collision_with_changed_content_halts():
    fake = FakeConn()
    outbox = V40TradeOutbox(fake)
    await outbox.enqueue(verified("ENTRY_FILLED"))
    with pytest.raises(V40EventInvalid, match="IDEMPOTENCY_COLLISION"):
        await outbox.enqueue(
            verified("ENTRY_FILLED", price=D("101.0"))
        )


@pytest.mark.asyncio
async def test_asyncpg_default_jsonb_string_codec_is_supported():
    import json
    class AsyncpgStringConn(FakeConn):
        async def fetchrow(self, statement, *args):
            r = await super().fetchrow(statement, *args)
            return {**r, "payload": json.dumps(r["payload"])} if r is not None else None
        async def fetch(self, statement, limit):
            rows = await super().fetch(statement, limit)
            return [
                {**r, "payload": json.dumps(r["payload"])} for r in rows
            ]
    store = V40TradeOutbox(AsyncpgStringConn())
    event = verified("ENTRY_FILLED")
    assert await store.enqueue(event) is True
    assert await store.enqueue(event) is True
    pending = await store.pending()
    assert len(pending) == 1
    assert pending[0][1]["title"] == event.discord_embed()["title"]
