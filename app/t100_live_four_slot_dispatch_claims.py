"""Irreversible, SQL-only V40 dispatch-claim fence. No exchange transport.

One successful claim means an order POST MAY have occurred. A competing
worker, new leader, or restart may never claim the same externalOid again.
An order missing on subsequent GET is NOT permission to retry. The claim is
a necessary database invariant for a future separately certified dispatcher,
not a live order permit, approved account preflight, or exchange guarantee.

Migration 032 is CREATE-ONLY and is NOT approved/applied on production.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_reservations import ACCOUNT_LOCK_SQL, LEASE_SQL
from app.t100_live_leader_fencing import LeaderLease

SLOT_SQL = """
SELECT signal_id, symbol, external_oid, holder_id, fencing_token,
       state, EXTRACT(EPOCH FROM (clock_timestamp() - snapshot_observed_at))
       AS witness_age_sec
FROM public.t100_live_v40_slot_reservations
WHERE signal_id=$1 FOR UPDATE
"""
INTENT_SQL = """
SELECT signal_id, symbol, external_oid, state
FROM public.t100_live_order_intents
WHERE signal_id=$1 FOR UPDATE
"""
CLAIM_SQL = """
INSERT INTO public.t100_live_v40_dispatch_claims
(signal_id,external_oid,holder_id,fencing_token)
VALUES($1,$2,$3,$4)
ON CONFLICT DO NOTHING RETURNING signal_id
"""


class FourSlotDispatchClaimHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class FourSlotDispatchClaim:
    signal_id: int
    external_oid: str
    state: str = "POST_POSSIBLE_UNRESOLVED"
    reconciliation_required: bool = True
    post_attempted: bool = False
    exchange_post_authorized: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only_exchange: bool = True


class PostgresFourSlotDispatchClaims:
    """Only an irreversible database claim. Never invoke a MEXC POST."""

    def __init__(self, pool: Any) -> None:
        if pool is None:
            raise ValueError("DISPATCH_CLAIM_DATABASE_POOL_REQUIRED")
        self.pool = pool

    async def claim(
        self, *, stage: DurableAttemptStage, lease: LeaderLease,
    ) -> FourSlotDispatchClaim:
        if (
            type(stage) is not DurableAttemptStage
            or type(stage.signal_id) is not int or stage.signal_id <= 0
            or type(stage.external_oid) is not str or not stage.external_oid
            or stage.state != "SUBMITTING"
            or stage.post_attempted is not False
            or stage.reconciliation_required is not True
            or stage.entry_allowed is not False
            or stage.live_authorization is not False
            or type(lease) is not LeaderLease
            or lease.lease_name != "t100-live-executor"
            or not lease.holder_id
            or type(lease.fencing_token) is not int
            or lease.fencing_token <= 0
            or lease.live_authorization is not False
        ):
            raise FourSlotDispatchClaimHalted(
                "FOUR_SLOT_DISPATCH_STAGE_OR_FENCE_INVALID"
            )
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(isolation="read_committed"):
                    await conn.execute(ACCOUNT_LOCK_SQL)
                    if await conn.fetchval(
                        "SELECT current_database()"
                    ) != EXPECTED_DATABASE:
                        raise FourSlotDispatchClaimHalted(
                            "FOUR_SLOT_DISPATCH_DATABASE_MISMATCH"
                        )
                    current = await conn.fetchrow(LEASE_SQL)
                    if (
                        current is None
                        or current["holder_id"] != lease.holder_id
                        or type(current["fencing_token"]) is not int
                        or current["fencing_token"] != lease.fencing_token
                        or current["unexpired"] is not True
                    ):
                        raise FourSlotDispatchClaimHalted(
                            "FOUR_SLOT_DISPATCH_LEASE_STALE_OR_UNVERIFIED"
                        )
                    slot = await conn.fetchrow(SLOT_SQL, stage.signal_id)
                    intent = await conn.fetchrow(INTENT_SQL, stage.signal_id)
                    if (
                        slot is None or intent is None
                        or slot["external_oid"] != stage.external_oid
                        or intent["external_oid"] != stage.external_oid
                        or slot["symbol"] != intent["symbol"]
                        or slot["holder_id"] != lease.holder_id
                        or type(slot["fencing_token"]) is not int
                        or slot["fencing_token"] != lease.fencing_token
                        or slot["state"] != "SUBMITTING"
                        or intent["state"] != "SUBMITTING"
                        or type(slot["signal_id"]) is not int
                        or slot["signal_id"] != stage.signal_id
                        or type(intent["signal_id"]) is not int
                        or intent["signal_id"] != stage.signal_id
                    ):
                        raise FourSlotDispatchClaimHalted(
                            "FOUR_SLOT_DISPATCH_DURABLE_STAGE_MISMATCH"
                        )
                    # Freshness is only a refusal gate, NOT proof that the
                    # exchange, wallet, order book or account is still current.
                    age = slot["witness_age_sec"]
                    if (
                        age is None
                        or not -0.25 <= float(age) <= 2.0
                    ):
                        raise FourSlotDispatchClaimHalted(
                            "FOUR_SLOT_DISPATCH_STAGED_WITNESS_STALE"
                        )
                    changed = await conn.fetchval(
                        CLAIM_SQL, stage.signal_id, stage.external_oid,
                        lease.holder_id, lease.fencing_token,
                    )
                    if changed != stage.signal_id:
                        raise FourSlotDispatchClaimHalted(
                            "FOUR_SLOT_DISPATCH_ALREADY_CLAIMED_RECONCILE_ONLY"
                        )
                    # Persisted claim cannot be deleted/released by this API.
                    # The future transport is a separate explicit approval.
                    return FourSlotDispatchClaim(
                        signal_id=stage.signal_id,
                        external_oid=stage.external_oid,
                    )
        except FourSlotDispatchClaimHalted:
            raise
        except Exception:
            raise FourSlotDispatchClaimHalted(
                "FOUR_SLOT_DISPATCH_DB_FAILURE_RECONCILE_ONLY"
            ) from None
