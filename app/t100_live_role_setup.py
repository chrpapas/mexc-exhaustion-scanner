"""Manually invoked, guarded T100 PostgreSQL runtime-role provisioning.

This script is never imported by the Render shadow or trader. Default is a
read-only PLAN. APPLY creates exactly one login without touching Render-managed
default credentials, grants ONLY pinned T100 privileges transactionally, then
opens a separate connection as the new role for effective-permission audit.

It cannot submit MEXC orders or authorize live trading.
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

from app.t100_live_role_preflight import (
    TARGET_ROLE,
    inspect_role_setup_preconditions,
)
from app.t100_live_db_inspect import EXPECTED_DATABASE, inspect_live_database

ROOT = Path(__file__).resolve().parents[1]
PRIVILEGES_PATH = "sql/t100_live_runtime_privileges_template.sql"
PINNED_GRANTS_GIT_BLOB = "4e34da1bb3167220fe9a4f2b1d07787782dee1a4"
ROLE_CONFIRM = "CREATE t100_live_executor_runtime ON mexc_exhaustion"


class RuntimeRoleSetupBlocked(RuntimeError):
    pass


def pinned_grants(root: Path = ROOT) -> str:
    data = (root / PRIVILEGES_PATH).read_bytes()
    digest = hashlib.sha1(
        b"blob " + str(len(data)).encode() + bytes([0]) + data
    ).hexdigest()
    if digest != PINNED_GRANTS_GIT_BLOB:
        raise RuntimeRoleSetupBlocked("RUNTIME_GRANTS_TEMPLATE_HASH_MISMATCH")
    template = data.decode("utf-8")
    if template.count("__T100_LIVE_RUNTIME_ROLE__") < 2:
        raise RuntimeRoleSetupBlocked("RUNTIME_GRANTS_ROLE_PLACEHOLDER_INVALID")
    return template.replace("__T100_LIVE_RUNTIME_ROLE__", TARGET_ROLE)


def _password_literal(password: str) -> str:
    # Only a PostgreSQL string literal is constructed. The role identifier
    # is fixed by source code, never operator-controlled.
    if not 32 <= len(password) <= 256:
        raise RuntimeRoleSetupBlocked("PASSWORD_LENGTH_OUT_OF_RANGE")
    if any(ord(c) < 32 or ord(c) > 126 for c in password):
        raise RuntimeRoleSetupBlocked("PASSWORD_MUST_BE_PRINTABLE_ASCII")
    return "'" + password.replace("'", "''") + "'"


async def _read_preflight(conn: Any) -> dict[str, Any]:
    async with conn.transaction(readonly=True):
        result = await inspect_role_setup_preconditions(conn)
    return result


async def provision_role(
    conn: Any, *, password: str, grants: str,
) -> dict[str, Any]:
    # Password is deliberately never returned or logged.
    literal = _password_literal(password)
    preflight = await _read_preflight(conn)
    if preflight.get("status") != "DIRECT_ROLE_CREATION_PREFLIGHT_PASSED":
        raise RuntimeRoleSetupBlocked(
            "ROLE_PREFLIGHT_BLOCKED:" + str(preflight.get("status"))
        )
    try:
        return await _provision_role_transaction(
            conn, password_literal=literal, grants=grants,
        )
    except RuntimeRoleSetupBlocked:
        raise
    except Exception as exc:
        # A failed CREATE ROLE can cause a driver error that embeds the SQL
        # statement (which contains the password). Never propagate it.
        raise RuntimeRoleSetupBlocked(
            "ROLE_TRANSACTION_FAILED:" + type(exc).__name__
        ) from None


async def _provision_role_transaction(
    conn: Any, *, password_literal: str, grants: str,
) -> dict[str, Any]:
    async with conn.transaction():
        await conn.execute("SET LOCAL search_path = public")
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        await conn.execute("SET LOCAL statement_timeout = '30s'")
        if await conn.fetchval("SELECT current_database()") != EXPECTED_DATABASE:
            raise RuntimeRoleSetupBlocked("WRONG_DATABASE_IDENTITY")
        locked = await conn.fetchval(
            "SELECT pg_try_advisory_xact_lock(110028, 110031)"
        )
        if locked is not True:
            raise RuntimeRoleSetupBlocked("ROLE_PROVISIONING_LOCK_UNAVAILABLE")
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=$1)",
            TARGET_ROLE,
        )
        if exists:
            raise RuntimeRoleSetupBlocked("RUNTIME_ROLE_ALREADY_EXISTS")
        # A fresh, independent SQL-created role does not change Render's
        # default database user/credentials or existing scanner connections.
        await conn.execute(
            "CREATE ROLE " + TARGET_ROLE
            + " WITH LOGIN NOINHERIT NOSUPERUSER NOCREATEDB"
            + " NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            + " CONNECTION LIMIT 4 PASSWORD " + password_literal
        )
        await conn.execute(grants)
        role = await conn.fetchrow(
            """SELECT rolcanlogin,rolinherit,rolsuper,rolcreatedb,
                      rolcreaterole,rolreplication,rolbypassrls
                 FROM pg_catalog.pg_roles WHERE rolname=$1""",
            TARGET_ROLE,
        )
        if role is None or role["rolcanlogin"] is not True or any(
            role[field] is not False for field in (
                "rolinherit", "rolsuper", "rolcreatedb",
                "rolcreaterole", "rolreplication", "rolbypassrls",
            )
        ):
            raise RuntimeRoleSetupBlocked("RUNTIME_ROLE_ATTRIBUTES_INVALID")
    return {
        "status": "RUNTIME_ROLE_CREATED_PENDING_INDEPENDENT_LOGIN_AUDIT",
        "database_name_verified": True,
        "role_name": TARGET_ROLE,
        "new_login_created": True,
        "default_render_database_login_unchanged": True,
        "grants_applied": True,
        "effective_privileges_verified": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
    }


async def independent_runtime_audit(conn: Any) -> dict[str, Any]:
    async with conn.transaction(readonly=True):
        actual_role = await conn.fetchval("SELECT current_user")
        if actual_role != TARGET_ROLE:
            return {
                "status": "CONNECTED_ROLE_IDENTITY_MISMATCH",
                "live_authorization": False,
            }
        audit = await inspect_live_database(conn)
        role_info = await conn.fetchrow(
            """SELECT rolcanlogin,rolinherit,rolsuper,rolcreatedb,
                      rolcreaterole,rolreplication,rolbypassrls
                 FROM pg_catalog.pg_roles WHERE rolname=current_user"""
        )
        flags_ok = role_info is not None and (
            role_info["rolcanlogin"] is True
            and all(role_info[k] is False for k in (
                "rolinherit", "rolsuper", "rolcreatedb",
                "rolcreaterole", "rolreplication", "rolbypassrls",
            ))
        )
        disallowed_create = await conn.fetchrow(
            """SELECT
               has_schema_privilege(current_user,'public','CREATE')
                 AS can_create_schema_objects,
               has_database_privilege(current_user,current_database(),'CREATE')
                 AS can_create_in_database"""
        )
        create_absent = (
            disallowed_create is not None
            and disallowed_create["can_create_schema_objects"] is False
            and disallowed_create["can_create_in_database"] is False
        )
        prod_rows = await conn.fetch(
            """SELECT c.relname AS table_name,
                 has_table_privilege(current_user,c.oid,'SELECT') AS can_select,
                 has_table_privilege(current_user,c.oid,'INSERT') AS can_insert,
                 has_table_privilege(current_user,c.oid,'UPDATE') AS can_update,
                 has_table_privilege(current_user,c.oid,'DELETE') AS can_delete,
                 has_table_privilege(current_user,c.oid,'TRUNCATE') AS can_truncate
               FROM pg_catalog.pg_class c
               JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public'
                 AND c.relkind IN ('r','p')
                 AND left(c.relname,10) <> 't100_live_'"""
        )
        prod_tables_with_privileges = sum(
            1 for row in prod_rows
            if any(row[field] is True for field in (
                "can_select", "can_insert", "can_update",
                "can_delete", "can_truncate",
            ))
        )
        clean = all((
            audit.get("status") == "LIVE_DATABASE_READ_ONLY_PRECHECK_PASSED",
            flags_ok,
            create_absent,
            prod_tables_with_privileges == 0,
        ))
        return {
            "status": (
                "RESTRICTED_T100_RUNTIME_ROLE_VERIFIED"
                if clean else "RESTRICTED_T100_RUNTIME_ROLE_VERIFICATION_FAILED"
            ),
            "role_name_verified": True,
            "new_role_attributes_verified": bool(flags_ok),
            "runtime_create_privileges_absent": bool(create_absent),
            "production_tables_inspected": len(prod_rows),
            "production_tables_with_privileges": prod_tables_with_privileges,
            "live_schema_and_runtime_privileges_verified": (
                audit.get("status") == "LIVE_DATABASE_READ_ONLY_PRECHECK_PASSED"
            ),
            "db_inspection_status": audit.get("status"),
            "db_inspection_failure_type": audit.get("failure_type"),
            "unresolved_mutations_absent": audit.get(
                "unresolved_mutations_absent", False
            ),
            "live_authorization": False,
            "mexc_orders_sent": False,
        }


async def _main(*, apply: bool) -> None:
    if os.getenv("T100_EXECUTION_MODE", "paper").strip().lower() != "paper":
        raise RuntimeRoleSetupBlocked("PAPER_MODE_REQUIRED")
    grants = pinned_grants()
    if apply:
        print(
            "Creates a NEW PostgreSQL user and grants only T100 live-table"
            " privileges; no existing Render default credentials are rotated."
        )
        if input(f"Type '{ROLE_CONFIRM}' to confirm: ").strip() != ROLE_CONFIRM:
            raise RuntimeRoleSetupBlocked("EXPLICIT_OPERATOR_APPROVAL_MISSING")
        password = getpass.getpass(
            "NEW runtime PostgreSQL password (32+ chars, hidden): "
        )
        confirmation = getpass.getpass("Confirm NEW runtime password: ")
        if password != confirmation:
            raise RuntimeRoleSetupBlocked("PASSWORD_CONFIRMATION_MISMATCH")
        _password_literal(password)
    else:
        password = ""

    dsn = getpass.getpass("Existing owner Render PostgreSQL URL (hidden): ")
    if not dsn:
        raise RuntimeRoleSetupBlocked("OWNER_DATABASE_URL_REQUIRED")
    import asyncpg
    try:
        conn = await asyncpg.connect(
            dsn=dsn, ssl="require", timeout=10, command_timeout=35,
        )
    except Exception as exc:
        raise RuntimeRoleSetupBlocked(
            "OWNER_DB_CONNECT_FAILED:" + type(exc).__name__
        ) from None
    try:
        if not apply:
            preflight = await _read_preflight(conn)
            print(json.dumps({
                "status": preflight.get("status"),
                "mode": "PLAN_READ_ONLY",
                "ready_for_role_creation": preflight.get("status")
                    == "DIRECT_ROLE_CREATION_PREFLIGHT_PASSED",
                "live_authorization": False,
            }, indent=2))
            return
        created = await provision_role(conn, password=password, grants=grants)
    finally:
        await conn.close()

    # Separate connection with the actual runtime login: owner-role ACL
    # checks can hide effective privileges or mistaken login assumptions.
    try:
        runtime = await asyncpg.connect(
            dsn=dsn,
            user=TARGET_ROLE,
            password=password,
            ssl="require",
            timeout=10,
            command_timeout=15,
        )
    except Exception as exc:
        print(json.dumps({
            **created,
            "status": "ROLE_CREATED_BUT_INDEPENDENT_LOGIN_FAILED",
            "login_failure_type": type(exc).__name__,
        }, sort_keys=True, indent=2))
        return
    try:
        try:
            verified = await independent_runtime_audit(runtime)
        except Exception as exc:
            verified = {
                "status": "RESTRICTED_T100_RUNTIME_ROLE_AUDIT_FAILED",
                "audit_failure_type": type(exc).__name__,
                "live_authorization": False,
            }
        print(json.dumps({
            **created,
            **verified,
        }, sort_keys=True, indent=2))
    finally:
        await runtime.close()
    print("ROLE SETUP ONLY. NO MEXC REQUESTS. LIVE TRADING IS DISABLED.")


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Manual-only T100 least-privilege database role setup"
    )
    parser.add_argument("--apply", action="store_true",
                        help="Explicitly create user and apply restricted GRANTs")
    args = parser.parse_args()
    try:
        asyncio.run(_main(apply=args.apply))
    except RuntimeRoleSetupBlocked as exc:
        print(json.dumps({
            "status": "ROLE_SETUP_BLOCKED",
            "reason": str(exc),
            "live_authorization": False,
        }, sort_keys=True))
        raise SystemExit(2) from None


if __name__ == "__main__":
    cli()
