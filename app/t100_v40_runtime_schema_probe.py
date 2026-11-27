"""Production-catalog V40 execution prerequisites, observed with SELECT only.

A running read-only worker reports whether required database objects exist.
This is NOT a migration, SQL-grant certification, exchange POST permit, or
live release. No credentials, table contents or exception details are logged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


READINESS_SQL = """
SELECT
  to_regclass('public.t100_live_final_position_accounting') IS NOT NULL
    AS final_accounting,
  to_regclass('public.t100_live_order_fill_sets') IS NOT NULL
    AS fill_sets,
  to_regclass('public.t100_live_position_funding_sets') IS NOT NULL
    AS funding_sets,
  to_regclass('public.t100_v40_exit_bar_cursor') IS NOT NULL
    AS exit_cursor,
  to_regclass('public.t100_live_v40_entry_contract_specs') IS NOT NULL
    AS at_entry_contract_specs,
  EXISTS(
    SELECT 1 FROM pg_catalog.pg_constraint c
    WHERE c.conrelid=to_regclass('public.t100_live_position_checkpoints')
      AND pg_catalog.pg_get_constraintdef(c.oid) LIKE '%BAR_OBSERVED%'
  ) AS checkpoint_bar_observed
"""


@dataclass(frozen=True)
class V40RuntimeCatalogEvidence:
    status: str
    catalog_observed: bool = False
    final_accounting_table_present: bool | None = None
    fill_sets_table_present: bool | None = None
    funding_sets_table_present: bool | None = None
    exit_cursor_table_present: bool | None = None
    at_entry_contract_specs_table_present: bool | None = None
    checkpoint_bar_observed_supported: bool | None = None
    # Presence is necessary, but NEVER sufficient for readiness: grants,
    # contract multipliers, exchange and end-to-end procedures still matter.
    execution_ready_certified: bool = False
    live_authorization: bool = False
    database_writes: int = 0
    mexc_posts: int = 0


async def observe_runtime_catalog(pool: Any) -> V40RuntimeCatalogEvidence:
    if pool is None or not callable(getattr(pool, "acquire", None)):
        return V40RuntimeCatalogEvidence("V40_SCHEMA_READONLY_POOL_MISSING")
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(READINESS_SQL)
        if row is None:
            raise ValueError()
        fields = (
            "final_accounting", "fill_sets", "funding_sets",
            "exit_cursor", "at_entry_contract_specs",
            "checkpoint_bar_observed",
        )
        values = tuple(row[k] for k in fields)
        if any(type(x) is not bool for x in values):
            raise ValueError()
        return V40RuntimeCatalogEvidence(
            status="V40_SCHEMA_CATALOG_OBSERVED_READONLY",
            catalog_observed=True,
            final_accounting_table_present=values[0],
            fill_sets_table_present=values[1],
            funding_sets_table_present=values[2],
            exit_cursor_table_present=values[3],
            at_entry_contract_specs_table_present=values[4],
            checkpoint_bar_observed_supported=values[5],
        )
    except Exception:
        return V40RuntimeCatalogEvidence(
            "V40_SCHEMA_READONLY_CATALOG_UNVERIFIED"
        )
