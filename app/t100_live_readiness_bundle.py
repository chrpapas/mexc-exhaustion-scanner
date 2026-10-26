"""One-shot read-only dynamic readiness bundle for T100.

This command combines the dynamic gates that can be measured directly from
the live database and MEXC without any exchange mutation:
- durable live schema + runtime privileges
- unresolved mutation/intervention DB state
- current account inventory completeness
- flat/non-flat account reconciliation

It never issues a LiveMutationPermit and always reports live_authorization=false.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from app.t100_live_account_reconciliation import (
    assess_account_reconciliation,
)
from app.t100_live_db_state_gate import assess_unresolved_live_state
from app.t100_live_guards import T100ExecutionControls
from app.t100_live_inventory_completeness_probe import (
    run_current_inventory_completeness_probe,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_schema_gate import assess_live_schema_and_privileges


async def run_dynamic_readiness_bundle(
    *,
    client: Any,
    pool: Any,
    durable_nonflat_recovery_verified: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "read_only": True,
        "live_order_submission_possible": False,
        "entry_allowed": False,
        "live_authorization": False,
    }
    if type(durable_nonflat_recovery_verified) is not bool:
        result["status"] = "DYNAMIC_READINESS_INPUT_INVALID"
        return result

    schema = await assess_live_schema_and_privileges(pool)
    state = await assess_unresolved_live_state(pool)
    inventory = await run_current_inventory_completeness_probe(
        ReadOnlyMexcProbe(client)
    )
    account = assess_account_reconciliation(
        inventory,
        durable_nonflat_recovery_verified=(
            durable_nonflat_recovery_verified
        ),
    )

    result.update({
        "durable_schema": schema,
        "durable_state": state,
        "current_inventory": inventory,
        "account_reconciliation": account,
        "durable_schema_deployed_verified": (
            schema.get("durable_schema_deployed_verified") is True
        ),
        "unresolved_mutations_absent": (
            state.get("unresolved_mutations_absent") is True
        ),
        "current_inventory_completeness_verified": (
            inventory.get("account_wide_completeness_verified") is True
        ),
        "account_reconciled": (
            account.get("account_reconciled") is True
        ),
    })
    blockers = []
    for key in (
        "durable_schema_deployed_verified",
        "unresolved_mutations_absent",
        "current_inventory_completeness_verified",
        "account_reconciled",
    ):
        if result[key] is not True:
            blockers.append(key)
    result["dynamic_blockers"] = blockers
    result["dynamic_readiness_verified"] = not blockers
    result["status"] = (
        "DYNAMIC_READINESS_VERIFIED_NON_AUTHORIZING"
        if not blockers
        else "DYNAMIC_READINESS_BLOCKED"
    )
    return result


async def _main() -> None:
    controls = T100ExecutionControls.from_env()
    if controls.mode != "paper":
        raise RuntimeError(
            "DYNAMIC_READINESS_CHECK_REFUSES_NON_PAPER_MODE"
        )

    database_url = os.getenv("DATABASE_URL") or ""
    api_key = os.getenv("MEXC_API_KEY") or ""
    api_secret = os.getenv("MEXC_API_SECRET") or ""
    if not database_url:
        raise RuntimeError("DATABASE_URL_REQUIRED")
    if not api_key or not api_secret:
        raise RuntimeError("MEXC_READ_CREDENTIALS_REQUIRED")

    import asyncpg
    from app.mexc_trade import MexcTradeClient

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
    client = MexcTradeClient(
        "https://api.mexc.com",
        api_key=api_key,
        api_secret=api_secret,
    )
    try:
        report = await run_dynamic_readiness_bundle(
            client=client,
            pool=pool,
            durable_nonflat_recovery_verified=False,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        print(
            "READ ONLY. NO ORDERS SENT. LIVE AUTHORIZATION REMAINS FALSE."
        )
    finally:
        await client.close()
        await pool.close()


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only T100 dynamic live-readiness bundle"
    )
    parser.parse_args()
    asyncio.run(_main())


if __name__ == "__main__":
    cli()
