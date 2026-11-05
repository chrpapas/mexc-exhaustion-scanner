"""SELECT-only durable T100 ownership source for a separate guardian observer.

No account is automatically enrolled. Runtime must explicitly pin both the
MEXC API-key SHA256 and position ID, and must use a transaction in READ ONLY
mode with a DB role that cannot mutate T100 tables. A current checkpoint
alone is insufficient: the full revision chain, entry intent ACK and all
owned stop amendments must agree.

No exchange calls or PostgreSQL writes. Never adopts manual positions.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import hmac
import re
from typing import Any

from app.t100_live_durable_recovery import (
    DurableRecoveryHalted, checkpoint_from_row, _validate_revision_history,
    _validate_intents, _validate_stop_amendments,
)
from app.t100_live_owned_emergency_guardian import OwnedShort, _validate_owned


class GuardianCheckpointHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class GuardianCheckpointRead:
    status: str
    owned: OwnedShort | None = None
    checkpoint_revisions_validated: bool = False
    account_scope_pinned: bool = False
    entry_intent_ack_verified: bool = False
    stop_amendments_resolved: bool = False
    live_authorization: bool = False


def _valid_pin(s: Any) -> bool:
    return type(s) is str and re.fullmatch("[a-f0-9]{64}", s) is not None


def check_api_scope(*, api_key: str, pinned_scope_sha256: str) -> None:
    """Do not log or persist the supplied credential."""
    if (type(api_key) is not str or len(api_key) < 12
            or not _valid_pin(pinned_scope_sha256)):
        raise GuardianCheckpointHalted("EXPLICIT_ACCOUNT_SCOPE_PIN_REQUIRED")
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, pinned_scope_sha256):
        raise GuardianCheckpointHalted("ACCOUNT_API_KEY_FINGERPRINT_MISMATCH")


async def _require_restricted_readonly_connection(conn: Any) -> None:
    """Defense in depth, beyond the caller's READ ONLY transaction."""
    try:
        row = await conn.fetchrow(
            """SELECT current_setting('transaction_read_only') AS read_only,
                      has_table_privilege(current_user,
                          'public.t100_live_position_checkpoints', 'INSERT')
                          AS cp_insert,
                      has_table_privilege(current_user,
                          'public.t100_live_position_checkpoints', 'UPDATE')
                          AS cp_update,
                      has_table_privilege(current_user,
                          'public.t100_live_position_checkpoints', 'DELETE')
                          AS cp_delete,
                      has_table_privilege(current_user,
                          'public.t100_live_order_intents', 'UPDATE')
                          AS intent_update,
                      has_table_privilege(current_user,
                          'public.t100_live_stop_amendments', 'UPDATE')
                          AS stop_update"""
        )
    except Exception as exc:
        raise GuardianCheckpointHalted("RESTRICTED_DB_ROLE_READ_UNVERIFIED") from exc
    if (row is None or row["read_only"] != "on" or
            any(row[key] is not False for key in (
                "cp_insert", "cp_update", "cp_delete",
                "intent_update", "stop_update"
            ))):
        raise GuardianCheckpointHalted("DB_ROLE_HAS_MUTATION_PRIVILEGES")


async def load_pinned_owned_checkpoint(
    conn: Any,
    *, api_key: str, pinned_scope_sha256: str,
    pinned_position_id: str,
) -> GuardianCheckpointRead:
    """Call inside a DB read-only transaction, using SELECT-only role."""
    check_api_scope(
        api_key=api_key, pinned_scope_sha256=pinned_scope_sha256,
    )
    if (type(pinned_position_id) is not str or
            re.fullmatch("[1-9][0-9]*", pinned_position_id) is None):
        raise GuardianCheckpointHalted("EXPLICIT_POSITION_PIN_REQUIRED")
    await _require_restricted_readonly_connection(conn)

    try:
        checkpoints = await conn.fetch(
            """SELECT symbol,external_oid,exchange_order_id,position_id,
                      contracts,entry_price,stop_order_id,stop_price,
                      best_profit_pct,armed,lifecycle_state,checkpoint_version
               FROM public.t100_live_position_checkpoints
               WHERE lifecycle_state <> 'CLOSED'
               ORDER BY position_id LIMIT 5"""
        )
        if not isinstance(checkpoints, list):
            raise GuardianCheckpointHalted("CHECKPOINT_FETCH_NOT_LIST")
        if not checkpoints:
            # No adoption from exchange list; no MEXC GET is needed.
            return GuardianCheckpointRead(
                status="NO_DURABLE_T100_POSITION_RECORDED",
                account_scope_pinned=True,
            )
        if len(checkpoints) != 1:
            raise GuardianCheckpointHalted(
                "MULTIPLE_DURABLE_POSITIONS_REQUIRE_EXPLICIT_SCOPE_MAPPING"
            )
        raw = dict(checkpoints[0])
        cp = checkpoint_from_row(raw)
        if cp.position_id != pinned_position_id:
            raise GuardianCheckpointHalted("PINNED_POSITION_ID_NOT_IN_DB")

        revisions = [
            dict(r) for r in await conn.fetch(
                """SELECT position_id,checkpoint_version,write_reason,
                          symbol,external_oid,exchange_order_id,contracts,
                          entry_price,stop_order_id,stop_price,best_profit_pct,
                          armed,lifecycle_state
                   FROM public.t100_live_position_checkpoint_revisions
                   WHERE position_id=$1 ORDER BY checkpoint_version""",
                cp.position_id,
            )
        ]
        intents = [
            dict(r) for r in await conn.fetch(
                """SELECT symbol,external_oid,exchange_order_id,
                          volume_contracts,stop_price,state
                   FROM public.t100_live_order_intents
                   WHERE external_oid=$1""",
                cp.external_oid,
            )
        ]
        amendments = [
            dict(r) for r in await conn.fetch(
                """SELECT external_id,symbol,position_id,stop_order_id,
                          verified_stop_order_id,previous_stop,proposed_stop,state
                   FROM public.t100_live_stop_amendments
                   WHERE position_id=$1""",
                cp.position_id,
            )
        ]

        _validate_revision_history((cp,), revisions)
        _validate_intents((cp,), intents)
        _validate_stop_amendments((cp,), revisions, amendments)
    except GuardianCheckpointHalted:
        raise
    except DurableRecoveryHalted as exc:
        raise GuardianCheckpointHalted(
            "DURABLE_CHECKPOINT_PROVENANCE_INVALID"
        ) from exc
    except Exception as exc:
        raise GuardianCheckpointHalted(
            "READ_ONLY_DURABLE_LEDGER_UNAVAILABLE"
        ) from exc

    owned = OwnedShort(
        scope_sha256=pinned_scope_sha256,
        symbol=cp.symbol,
        position_id=cp.position_id,
        entry_external_oid=cp.external_oid,
        entry_order_id=cp.exchange_order_id,
        contracts=cp.contracts,
        stop_order_id=cp.stop_order_id,
        stop_price=cp.stop_price,
        checkpoint_version=cp.checkpoint_version,
        checkpoint_lifecycle=cp.lifecycle_state,
    )
    try:
        _validate_owned(owned, pinned_scope_sha256)
    except Exception as exc:
        raise GuardianCheckpointHalted(
            "DURABLE_ACCOUNT_OWNERSHIP_SHAPE_UNVERIFIED"
        ) from exc
    return GuardianCheckpointRead(
        status="DURABLE_OWNED_CHECKPOINT_PROVENANCE_VERIFIED_READ_ONLY",
        owned=owned, account_scope_pinned=True,
        checkpoint_revisions_validated=True,
        entry_intent_ack_verified=True,
        stop_amendments_resolved=True,
    )
