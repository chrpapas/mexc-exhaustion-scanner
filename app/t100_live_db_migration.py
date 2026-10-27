"""Opt-in, atomic T100-only schema migration. Never runs from a worker.

Default is PLAN (READ ONLY). APPLY requires a verified operator-made backup
export, explicit typed acknowledgement, pinned migration-file content, and an
empty live namespace on the exact named existing Postgres DB. APPLY creates
only the tables/indexes in migrations 027-030 in ONE transaction.

This does NOT provision the separate least-privilege runtime role, test leader
fencing, certify exchange execution, or authorize live trading.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from app.t100_live_db_inspect import (
    EXPECTED_DATABASE,
    LEASE_COLUMNS,
    LEASE_TABLE,
    REQUIRED_STRATEGY_INDEXES,
    STRATEGY_TABLE_COLUMNS,
)
from app.t100_live_schema_gate import (
    LIVE_TABLE_COLUMNS,
    REQUIRED_INDEXES,
    REQUIRED_SEQUENCES,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    ("migrations/027_t100_live_order_intents.sql",
     "4248f3fba666f53108048360547af202e3b6fe2e"),
    ("migrations/028_t100_live_vol30_h4.sql",
     "74e7b98a81ca7a8dcd2e3e1040c36b9aa341cf68"),
    ("migrations/029_t100_live_durable_state.sql",
     "2ee4b68e2263f187c099d2ccb9cde708f172f6a2"),
    ("migrations/030_t100_live_executor_lease.sql",
     "365e6e1edfa0b0f7498c6f437ad2521be60d5f38"),
)
LIVE_TABLES = frozenset(LIVE_TABLE_COLUMNS) | frozenset(
    STRATEGY_TABLE_COLUMNS
) | {LEASE_TABLE}
LIVE_COLUMNS = dict(LIVE_TABLE_COLUMNS)
LIVE_COLUMNS.update(STRATEGY_TABLE_COLUMNS)
LIVE_COLUMNS[LEASE_TABLE] = LEASE_COLUMNS
INDEXES = REQUIRED_INDEXES | REQUIRED_STRATEGY_INDEXES
SEQUENCES = REQUIRED_SEQUENCES | {"t100_live_vol30_children_id_seq"}

CONFIRMATION_TEXT = "APPLY 027-030 TO mexc_exhaustion"


class LiveMigrationBlocked(RuntimeError):
    pass


def _git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(content)).encode() + bytes([0]) + content
    ).hexdigest()


def load_pinned_migrations(root: Path = REPO_ROOT) -> tuple[bytes, ...]:
    blobs: list[bytes] = []
    for relative_path, expected_sha in MIGRATIONS:
        data = (root / relative_path).read_bytes()
        if _git_blob_sha(data) != expected_sha:
            raise LiveMigrationBlocked(
                "PINNED_MIGRATION_CONTENT_MISMATCH:" + relative_path
            )
        blobs.append(data)
    return tuple(blobs)


def classify_live_namespace(present: set[str]) -> str:
    """An entirely empty live namespace is the only automatic apply case."""
    if not present:
        return "SAFE_EMPTY_LIVE_NAMESPACE"
    if present == set(LIVE_TABLES):
        return "ALL_LIVE_TABLES_ALREADY_PRESENT_REVIEW_INSTEAD"
    return "PARTIAL_OR_UNEXPECTED_LIVE_NAMESPACE_REVIEW_INSTEAD"


async def _tables(conn: Any) -> set[str]:
    rows = await conn.fetch(
        """SELECT table_name FROM information_schema.tables
           WHERE table_schema='public'
             AND left(table_name,10)='t100_live_'"""
    )
    return {str(row["table_name"]) for row in rows}


async def _assert_target(conn: Any) -> None:
    actual = await conn.fetchval("SELECT current_database()")
    if actual != EXPECTED_DATABASE:
        raise LiveMigrationBlocked("WRONG_DATABASE_IDENTITY")


async def verify_migrated_structure(conn: Any) -> dict[str, Any]:
    present = await _tables(conn)
    if present != set(LIVE_TABLES):
        raise LiveMigrationBlocked("POST_DDL_TABLE_SET_MISMATCH")
    columns = await conn.fetch(
        """SELECT table_name,column_name FROM information_schema.columns
           WHERE table_schema='public'
             AND table_name = ANY($1::text[])""",
        sorted(LIVE_TABLES),
    )
    found: dict[str, set[str]] = {name: set() for name in LIVE_TABLES}
    for row in columns:
        name = str(row["table_name"])
        if name in found:
            found[name].add(str(row["column_name"]))
    if any(not required <= found[name] for name, required in LIVE_COLUMNS.items()):
        raise LiveMigrationBlocked("POST_DDL_REQUIRED_COLUMNS_MISSING")
    rows = await conn.fetch(
        """SELECT indexname FROM pg_indexes
           WHERE schemaname='public'
             AND indexname = ANY($1::text[])""",
        sorted(INDEXES),
    )
    if not INDEXES <= {str(row["indexname"]) for row in rows}:
        raise LiveMigrationBlocked("POST_DDL_REQUIRED_INDEXES_MISSING")
    rows = await conn.fetch(
        """SELECT sequence_name FROM information_schema.sequences
           WHERE sequence_schema='public'
             AND sequence_name = ANY($1::text[])""",
        sorted(SEQUENCES),
    )
    if not SEQUENCES <= {str(row["sequence_name"]) for row in rows}:
        raise LiveMigrationBlocked("POST_DDL_REQUIRED_SEQUENCES_MISSING")
    return {
        "tables_verified": len(present),
        "required_columns_present": True,
        "required_indexes_present": True,
        "required_sequences_present": True,
    }


async def inspect_or_apply(conn: Any, *, apply: bool, blobs: tuple[bytes, ...]) -> dict[str, Any]:
    # A single transaction guarantees all-or-nothing DDL on PostgreSQL.
    async with conn.transaction(readonly=not apply):
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        await conn.execute("SET LOCAL statement_timeout = '30s'")
        await _assert_target(conn)
        if apply:
            acquired = await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock(110027, 110030)"
            )
            if acquired is not True:
                raise LiveMigrationBlocked("MIGRATION_LOCK_UNAVAILABLE")

        present = await _tables(conn)
        mode = classify_live_namespace(present)
        result: dict[str, Any] = {
            "mode": "APPLY" if apply else "PLAN_READ_ONLY",
            "database_name_verified": True,
            "live_tables_present": len(present),
            "expected_live_tables": len(LIVE_TABLES),
            "namespace_status": mode,
            "migrations": [p for p, _ in MIGRATIONS],
            "live_authorization": False,
            "orders_sent": False,
        }
        if not apply:
            result["status"] = (
                "MIGRATION_PLAN_READY_AFTER_BACKUP"
                if mode == "SAFE_EMPTY_LIVE_NAMESPACE"
                else "MIGRATION_PLAN_MANUAL_REVIEW_REQUIRED"
            )
            return result
        if mode != "SAFE_EMPTY_LIVE_NAMESPACE":
            raise LiveMigrationBlocked(mode)
        if len(blobs) != len(MIGRATIONS):
            raise LiveMigrationBlocked("MIGRATION_BLOBS_MISSING")
        for blob in blobs:
            await conn.execute(blob.decode("utf-8"))
        result.update(await verify_migrated_structure(conn))
        result["status"] = "T100_LIVE_TABLES_APPLIED_ATOMICALLY"
        result["runtime_role_privileges_verified"] = False
        return result


async def _main(args: argparse.Namespace) -> None:
    if os.getenv("T100_EXECUTION_MODE", "paper").strip().lower() != "paper":
        raise LiveMigrationBlocked("PAPER_MODE_REQUIRED")
    if args.apply and not args.backup_export_verified:
        raise LiveMigrationBlocked("RENDER_BACKUP_EXPORT_REQUIRED")
    # Check pinned content before even connecting to the production database.
    blobs = load_pinned_migrations()
    if args.apply:
        print(
            "APPLY requires an existing verified Render logical backup export. "
            "The shared production database will gain 12 T100-only tables."
        )
        if input("Type '" + CONFIRMATION_TEXT + "' to proceed: ").strip() != (
            CONFIRMATION_TEXT
        ):
            raise LiveMigrationBlocked("EXPLICIT_OPERATOR_APPROVAL_MISSING")

    dsn = getpass.getpass("Render PostgreSQL External URL (hidden): ")
    if not dsn:
        raise LiveMigrationBlocked("DATABASE_URL_REQUIRED")
    import asyncpg
    try:
        conn = await asyncpg.connect(
            dsn=dsn,
            ssl="require",
            timeout=10,
            command_timeout=35,
        )
    except Exception as exc:
        raise LiveMigrationBlocked(
            "DATABASE_CONNECT_FAILED:" + type(exc).__name__
        ) from None

    try:
        result = await inspect_or_apply(conn, apply=args.apply, blobs=blobs)
        print(json.dumps(result, sort_keys=True, indent=2))
        print("NO MEXC REQUESTS. NO LIVE TRADING AUTHORIZATION.")
    finally:
        await conn.close()


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Guarded, optional atomic T100-only PostgreSQL migrations"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--plan", action="store_true", help="Read-only plan (default)")
    group.add_argument("--apply", action="store_true", help="Atomic create-only DDL")
    parser.add_argument(
        "--backup-export-verified",
        action="store_true",
        help="Operator attests completed, verified Render logical export",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_main(args))
    except LiveMigrationBlocked as exc:
        print(json.dumps({
            "status": "MIGRATION_BLOCKED",
            "reason": str(exc),
            "live_authorization": False,
        }, sort_keys=True))
        raise SystemExit(2) from None


if __name__ == "__main__":
    cli()
