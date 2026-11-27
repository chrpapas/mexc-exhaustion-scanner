"""Unreleased V40 Discord outbox sender, intentionally NOT a Render entrypoint.

Business notifications are downstream of verified facts. This worker cannot
open positions, amend stops, close orders, or grant live mutation permits.
Delivery is at least once: a webhook ACK followed by a DB commit failure can
cause an explicitly labeled duplicate when the pending item is retried.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_v40_unreleased_trade_events import V40TradeOutbox, V40EventInvalid


@dataclass(frozen=True)
class V40DeliveryReport:
    pending_observed: int
    acknowledged: int
    unsuccessful: int
    exchange_posts: int = 0
    live_authorization: bool = False


async def deliver_verified_business_events_once(
    *,
    outbox: V40TradeOutbox,
    sender: Any,
    limit: int = 10,
) -> V40DeliveryReport:
    """Run from a separately reviewed single-instance service only.

    Caller MUST acquire the outbox's advisory singleton lock before first
    invocation and never share its SQL session across concurrent dispatchers.
    """
    if (
        type(outbox) is not V40TradeOutbox
        or not callable(getattr(sender, "send", None))
        or type(limit) is not int
        or not 1 <= limit <= 20
    ):
        raise V40EventInvalid("V40_TRADE_DISPATCH_INPUT_INVALID")
    batch = await outbox.pending(limit)
    delivered = 0
    failed = 0
    for key, embed in batch:
        if not (
            type(embed) is dict
            and type(embed.get("title")) is str
            and type(embed.get("description")) is str
            and type(embed.get("fields")) is list
            and type(embed.get("color")) is int
        ):
            raise V40EventInvalid("V40_TRADE_DISPATCH_UNTRUSTED_PAYLOAD")
        try:
            sent = await sender.send(
                embed["title"], embed["description"],
                embed["fields"], color=embed["color"],
            )
        except Exception:
            sent = False
        if sent is not True:
            # Preserve pending event; do not fabricate delivery or drop it.
            failed += 1
            break
        await outbox.mark_delivered(key)
        delivered += 1
    return V40DeliveryReport(
        pending_observed=len(batch), acknowledged=delivered,
        unsuccessful=failed,
    )
