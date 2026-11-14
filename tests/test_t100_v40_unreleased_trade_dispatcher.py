"""Discord outbox sender must fail closed and never place an order."""
import json

import pytest

from app.t100_v40_unreleased_trade_dispatcher import (
    deliver_verified_business_events_once,
)
from app.t100_v40_unreleased_trade_events import (
    V40BusinessEvent, V40TradeOutbox, V40EventInvalid,
)


class DummyConn:
    def __init__(self):
        self.rows = {}
        self.delivered = []

    async def fetchrow(self, sql, *params):
        if sql.startswith("INSERT"):
            event_key, kind, payload = params
            if event_key in self.rows:
                return None
            row = {"event_kind": kind, "payload": json.loads(payload)}
            self.rows[event_key] = row
            return row
        return self.rows.get(params[0])

    async def fetch(self, sql, limit):
        return [
            {"event_key": k, "payload": v["payload"]}
            for k, v in self.rows.items()
            if k not in self.delivered
        ][:limit]

    async def fetchval(self, sql, *params):
        if sql.startswith("SELECT pg_try"):
            return True
        self.delivered.append(params[0])
        return params[0]

    async def execute(self, sql, *args):
        return "OK"


class Sender:
    def __init__(self, outcome=True):
        self.outcome = outcome
        self.calls = []

    async def send(self, title, description, fields, *, color):
        self.calls.append((title, description, fields, color))
        return self.outcome


@pytest.mark.asyncio
async def test_success_acks_one_verified_event_and_never_posts_exchange_order():
    db = DummyConn()
    outbox = V40TradeOutbox(db)
    await outbox.acquire_singleton()
    event = V40BusinessEvent(
        kind="STAGE2_QUALIFIED",symbol="EXAMPLE_USDT",signal_id=5,
        evidence_id="stage2:5",evidence_status="V40_STAGE2_PARITY_PASS",
        observed_at_utc="2026-11-13T14:00:00Z",
    )
    await outbox.enqueue(event)
    sender = Sender()
    report = await deliver_verified_business_events_once(
        outbox=outbox,sender=sender
    )
    assert report.acknowledged == 1
    assert report.exchange_posts == 0
    assert report.live_authorization is False
    assert db.delivered == [event.idempotency_key]
    assert sender.calls[0][-1] == 0xFEE75C


@pytest.mark.asyncio
async def test_failed_webhook_keeps_event_pending():
    db = DummyConn()
    outbox = V40TradeOutbox(db)
    event = V40BusinessEvent(
        kind="STAGE2_QUALIFIED",symbol="EXAMPLE_USDT",signal_id=5,
        evidence_id="stage2:5",evidence_status="V40_STAGE2_PARITY_PASS",
        observed_at_utc="2026-11-13T14:00:00Z",
    )
    await outbox.enqueue(event)
    report = await deliver_verified_business_events_once(
        outbox=outbox,sender=Sender(False)
    )
    assert report.acknowledged == 0
    assert report.unsuccessful == 1
    assert not db.delivered
    assert len(await outbox.pending()) == 1


@pytest.mark.asyncio
async def test_invalid_dispatch_does_not_attempt_discord():
    with pytest.raises(V40EventInvalid, match="INPUT_INVALID"):
        await deliver_verified_business_events_once(
            outbox=None,sender=Sender()
        )
