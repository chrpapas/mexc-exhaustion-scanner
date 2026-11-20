"""Read-only production catalog check of proposed V40 migration owner.

This probe does NOT authenticate as the owner, export credentials, execute DDL,
apply migrations, or grant live trading. It reads catalog grants using the
existing dedicated SELECT-only runtime connection in a read-only transaction.
Render identifies mexc_scanner as the primary DB login; that is a CANDIDATE,
not proof of ownership or write credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE

CANDIDATE_ROLE="mexc_scanner"
RUNTIME_ROLE="t100_live_executor_runtime"
OWNER_SQL="""
SELECT
    current_database() AS database_name,
    EXISTS(
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=$1
    ) AS candidate_role_exists,
    EXISTS(
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=$2
    ) AS runtime_role_exists,
    CASE WHEN EXISTS(
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=$1
    ) THEN has_schema_privilege($1, 'public', 'CREATE')
      ELSE FALSE END AS candidate_can_create_public,
    (
        SELECT pg_catalog.pg_get_userbyid(c.relowner)
        FROM pg_catalog.pg_class c
        WHERE c.oid=to_regclass('public.t100_live_position_checkpoints')
    ) AS checkpoint_table_owner,
    (
        SELECT pg_catalog.pg_get_userbyid(c.relowner)
        FROM pg_catalog.pg_class c
        WHERE c.oid=to_regclass('public.t100_live_position_checkpoint_revisions')
    ) AS revision_table_owner,
    CASE WHEN EXISTS(
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=$1
    ) AND to_regclass('public.t100_live_v40_slot_reservations') IS NOT NULL
      THEN has_table_privilege(
        $1, 'public.t100_live_v40_slot_reservations', 'REFERENCES'
      )
      ELSE FALSE END AS candidate_can_reference_slot_reservations
"""


@dataclass(frozen=True)
class V40OwnerRoleEvidence:
    status: str
    catalog_observed: bool = False
    database_identity_verified: bool = False
    candidate_role: str = CANDIDATE_ROLE
    candidate_role_exists: bool | None = None
    runtime_role_exists: bool | None = None
    candidate_can_create_public: bool | None = None
    checkpoint_table_owner: str | None = None
    revision_table_owner: str | None = None
    candidate_can_reference_slot_reservations: bool | None = None
    owner_role_permissions_sufficient_on_catalog: bool = False
    authenticated_as_owner: bool = False
    production_ddl_applied: bool = False
    execution_ready_certified: bool = False
    live_authorization: bool = False
    database_writes: int = 0
    mexc_posts: int = 0


async def observe_owner_role_catalog(pool:Any)->V40OwnerRoleEvidence:
    if pool is None or not callable(getattr(pool,"acquire",None)):
        return V40OwnerRoleEvidence("V40_OWNER_ROLE_READONLY_POOL_REQUIRED")
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(
                isolation="repeatable_read",readonly=True,
            ):
                row=await conn.fetchrow(
                    OWNER_SQL,CANDIDATE_ROLE,RUNTIME_ROLE,
                )
        if row is None:
            raise ValueError()
        names=(
            "candidate_role_exists","runtime_role_exists",
            "candidate_can_create_public",
            "candidate_can_reference_slot_reservations",
        )
        if any(type(row[name]) is not bool for name in names):
            raise ValueError()
        owners=(
            row["checkpoint_table_owner"],row["revision_table_owner"],
        )
        if any(owner is not None and (
            type(owner) is not str or not owner or len(owner)>128
        ) for owner in owners):
            raise ValueError()
        database_ok=row["database_name"]==EXPECTED_DATABASE
        if type(row["database_name"]) is not str:
            raise ValueError()
        sufficient=all((
            database_ok,row["candidate_role_exists"],
            row["runtime_role_exists"],
            row["candidate_can_create_public"],
            row["candidate_can_reference_slot_reservations"],
            all(owner==CANDIDATE_ROLE for owner in owners),
        ))
        return V40OwnerRoleEvidence(
            status="V40_OWNER_ROLE_CATALOG_OBSERVED_READONLY",
            catalog_observed=True,
            database_identity_verified=database_ok,
            candidate_role_exists=row["candidate_role_exists"],
            runtime_role_exists=row["runtime_role_exists"],
            candidate_can_create_public=row["candidate_can_create_public"],
            checkpoint_table_owner=owners[0],
            revision_table_owner=owners[1],
            candidate_can_reference_slot_reservations=
                row["candidate_can_reference_slot_reservations"],
            owner_role_permissions_sufficient_on_catalog=bool(sufficient),
        )
    except Exception:
        return V40OwnerRoleEvidence(
            "V40_OWNER_ROLE_READONLY_CATALOG_UNVERIFIED"
        )
