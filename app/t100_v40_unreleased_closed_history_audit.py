"""Audited closed V40 history for genuinely flat account restart (SELECT only).

A flat exchange does NOT authorize resetting old order histories. To
recognize a previously traded but now truly flat account, every historical
CLOSED intent must match: immutable CLOSED position/checkpoint revisions,
complete final fill+funding accounting and an explicitly RELEASED V40 slot.
Any other journal row, unresolved native stop or missing closed provenance
halts. One repeatable-read SELECT-only transaction; no mutation, order
submission, live permit, or manual position adoption.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
from typing import Any, Sequence

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE


class V40ArchivedHistoryHalted(RuntimeError):
    pass


ARCHIVE_COUNTS_SQL = """
SELECT
 (SELECT COUNT(*)::int FROM public.t100_live_order_intents) AS intents_total,
 (SELECT COUNT(*)::int FROM public.t100_live_order_intents
   WHERE state='CLOSED') AS intents_closed,
 (SELECT COUNT(*)::int FROM public.t100_live_position_checkpoints) AS checkpoints_total,
 (SELECT COUNT(*)::int FROM public.t100_live_position_checkpoints
   WHERE lifecycle_state='CLOSED') AS checkpoints_closed,
 (SELECT COUNT(*)::int FROM public.t100_live_v40_slot_reservations) AS reservations_total,
 (SELECT COUNT(*)::int FROM public.t100_live_v40_slot_reservations
   WHERE state='RELEASED') AS reservations_released,
 (SELECT COUNT(*)::int FROM public.t100_live_final_position_accounting) AS final_total,
 (SELECT COUNT(*)::int FROM public.t100_live_stop_amendments
   WHERE state<>'VERIFIED') AS unresolved_stops
"""

ARCHIVE_ROWS_SQL = """
SELECT
 i.external_oid AS intent_oid, i.symbol AS intent_symbol,
 i.exchange_order_id AS intent_entry_id, i.state AS intent_state,
 c.position_id AS position_id,c.external_oid AS checkpoint_oid,
 c.symbol AS checkpoint_symbol,c.exchange_order_id AS checkpoint_entry_id,
 c.stop_order_id AS checkpoint_stop_id,c.contracts AS checkpoint_contracts,
 c.lifecycle_state AS checkpoint_state,c.last_write_reason AS checkpoint_reason,
 c.checkpoint_version AS checkpoint_version,
 r.external_oid AS reservation_oid,r.symbol AS reservation_symbol,
 r.state AS reservation_state,
 a.position_id AS accounting_position_id,a.entry_order_id AS accounting_entry_id,
 a.close_order_id AS accounting_close_id,a.stop_order_id AS accounting_stop_id,
 a.opened_contracts AS accounting_opened,
 a.closed_contracts AS accounting_closed,
 a.realized_net_usdt AS accounting_net,
 (SELECT COUNT(*)::int FROM public.t100_live_position_checkpoint_revisions v
  WHERE v.position_id=c.position_id) AS revision_total,
 (SELECT v.checkpoint_version FROM public.t100_live_position_checkpoint_revisions v
  WHERE v.position_id=c.position_id
  ORDER BY v.checkpoint_version DESC LIMIT 1) AS last_revision_version,
 (SELECT v.write_reason FROM public.t100_live_position_checkpoint_revisions v
  WHERE v.position_id=c.position_id
  ORDER BY v.checkpoint_version DESC LIMIT 1) AS last_revision_reason,
 (SELECT v.write_reason FROM public.t100_live_position_checkpoint_revisions v
  WHERE v.position_id=c.position_id
  ORDER BY v.checkpoint_version DESC OFFSET 1 LIMIT 1) AS previous_revision_reason
FROM public.t100_live_order_intents i
LEFT JOIN public.t100_live_position_checkpoints c
  ON c.external_oid=i.external_oid
LEFT JOIN public.t100_live_v40_slot_reservations r
  ON r.external_oid=i.external_oid
LEFT JOIN public.t100_live_final_position_accounting a
  ON a.position_id=c.position_id
WHERE i.state='CLOSED'
ORDER BY i.external_oid
"""


@dataclass(frozen=True)
class V40ArchivedHistoryAudit:
    status: str
    verified_closed_positions: int = 0
    every_archived_intent_accounted: bool = False
    every_immutable_close_and_release_verified: bool = False
    journal_unresolved_absent: bool = False
    account_flat_signed_get_verified: bool = False
    new_entry_allowed: bool = False
    live_authorization: bool = False
    database_writes: int = 0
    mexc_posts: int = 0


def _decimal_positive(value: Any) -> bool:
    if type(value) is not D or not value.is_finite() or value<=0:
        return False
    return True


def verify_closed_archive_rows(
    *, expected_intents: Sequence[dict[str,Any]],
    counts: dict[str,Any], rows: Sequence[dict[str,Any]],
) -> V40ArchivedHistoryAudit:
    halt=lambda x:V40ArchivedHistoryAudit(x)
    keys=("intents_total","intents_closed","checkpoints_total",
          "checkpoints_closed","reservations_total","reservations_released",
          "final_total","unresolved_stops")
    if (
        type(expected_intents) not in (list,tuple)
        or type(counts) is not dict
        or type(rows) not in (list,tuple)
        or any(type(x) is not dict for x in expected_intents)
        or any(type(x) is not dict for x in rows)
        or set(counts)!=set(keys)
        or any(type(counts[k]) is not int or counts[k]<0 for k in keys)
    ):
        return halt("V40_ARCHIVED_HISTORY_SOURCE_INVALID")
    n=counts["intents_closed"]
    if (
        n<1
        or len(rows)!=n
        or len(expected_intents)!=n
        or counts["unresolved_stops"]!=0
        or any(counts[k]!=n for k in (
            "intents_total","checkpoints_total","checkpoints_closed",
            "reservations_total","reservations_released","final_total"
        ))
    ):
        return halt("V40_ARCHIVED_HISTORY_ACCOUNTING_COUNT_MISMATCH")
    expected={}
    for item in expected_intents:
        oid=item.get("external_oid")
        if (
            type(oid) is not str or not oid
            or oid in expected
            or item.get("state")!="CLOSED"
            or type(item.get("symbol")) is not str
            or type(item.get("exchange_order_id")) is not str
            or not item.get("exchange_order_id")
        ):
            return halt("V40_ARCHIVED_HISTORY_INTENT_IDENTITY_INVALID")
        expected[oid]=(item["symbol"],item["exchange_order_id"])
    actual_ids=set()
    actual_positions=set()
    for row in rows:
        oid=row.get("intent_oid")
        pos=row.get("position_id")
        if (
            type(oid) is not str or oid not in expected or oid in actual_ids
            or type(pos) is not str or not pos or pos in actual_positions
            or row.get("intent_state")!="CLOSED"
            or row.get("intent_symbol")!=expected[oid][0]
            or row.get("intent_entry_id")!=expected[oid][1]
            or row.get("checkpoint_oid")!=oid
            or row.get("checkpoint_symbol")!=expected[oid][0]
            or row.get("checkpoint_entry_id")!=expected[oid][1]
            or row.get("checkpoint_state")!="CLOSED"
            or row.get("checkpoint_reason")!="CLOSE_VERIFIED"
            or row.get("reservation_oid")!=oid
            or row.get("reservation_symbol")!=expected[oid][0]
            or row.get("reservation_state")!="RELEASED"
            or row.get("accounting_position_id")!=pos
            or row.get("accounting_entry_id")!=expected[oid][1]
            or row.get("accounting_stop_id")!=row.get("checkpoint_stop_id")
            or type(row.get("accounting_close_id")) is not str
            or not row["accounting_close_id"]
            or not _decimal_positive(row.get("checkpoint_contracts"))
            or row.get("accounting_opened")!=row["checkpoint_contracts"]
            or row.get("accounting_closed")!=row["checkpoint_contracts"]
            or type(row.get("accounting_net")) is not D
            or not row["accounting_net"].is_finite()
            or type(row.get("checkpoint_version")) is not int
            or row["checkpoint_version"]<3
            or row.get("revision_total")!=row["checkpoint_version"]
            or row.get("last_revision_version")!=row["checkpoint_version"]
            or row.get("last_revision_reason")!="CLOSE_VERIFIED"
            or row.get("previous_revision_reason")!="CLOSING_LATCHED"
        ):
            return halt("V40_ARCHIVED_HISTORY_CLOSE_IDENTITY_OR_REVISION_INVALID")
        actual_ids.add(oid)
        actual_positions.add(pos)
    if actual_ids!=set(expected):
        return halt("V40_ARCHIVED_HISTORY_INTENT_SET_MISMATCH")
    return V40ArchivedHistoryAudit(
        status="V40_ALL_ARCHIVED_NATIVE_CLOSES_ACCOUNTED_NONAUTHORIZING",
        verified_closed_positions=n,
        every_archived_intent_accounted=True,
        every_immutable_close_and_release_verified=True,
        journal_unresolved_absent=True,
    )
