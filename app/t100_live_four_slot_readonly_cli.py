"""Opt-in signed-GET + restricted PostgreSQL four-slot ownership diagnostic.

Manual command only; never starts on Render automatically. Requires explicit
paper mode, live API disabled, and kill switch ENGAGED. The database reader
uses the existing restricted T100 role, TLS and SELECT-only repeatable-read
transactions. No DB mutation, MEXC POST or account state changes.

The current manual position is NOT assumed to be owned by T100. If it exists
without a matching durable T100 checkpoint, report UNOWNED and block.

The actual network client has POST methods elsewhere in the repository, but
only a deliberately narrow GET-only wrapper is ever passed to this flow.
Credentials/DSN are NEVER printed, sent in chat or saved to disk.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from typing import Any, Mapping

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)
from app.t100_live_four_slot_mexc_readonly import inspect_existing_four_slots_get_only


def _report(
    status: str, *, slots: int | None = None,
    mismatch: dict[str, Any] | None = None,
    manual_positions: int | None = None,
    unprotected_manual_positions: int | None = None,
    account_positions: int | None = None,
) -> dict[str, Any]:
    result = {
        "status": status,
        "owned_four_slot_positions_observed": (
            slots if type(slots) is int and 0 <= slots <= 4 else None
        ),
        "current_account_only_nonatomic": True,
        "native_stop_exchange_atomicity_certified": False,
        "cross_margin_liquidation_certified": False,
        "durable_entry_executor_certified": False,
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
        "database_writes_sent": False,
        "read_only": True,
    }
    if status in {
        "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
        "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
    }:
        # Counts only. Nothing returned here certifies CROSS collateral or
        # guarantees native stop execution. Manual SL is never required.
        for key, value in (
            ("manual_positions_observed", manual_positions),
            ("manual_positions_without_positive_sl_observed",
             unprotected_manual_positions),
            ("account_positions_observed", account_positions),
        ):
            result[key] = (
                value if type(value) is int and 0 <= value <= 4 else None
            )
        result["manual_positions_adopted_by_t100"] = False
        result["cross_account_risk_certified"] = False
    if status == "POSITION_STOP_COUNT_MISMATCH":
        # Explicit output allowlist. No raw MEXC rows, IDs, symbols, prices
        # or sensitive identifiers. Other errors never trigger this field.
        aggregate = {}
        if type(mismatch) is dict and mismatch.get("status") in {
            "POSITION_STOP_TOPOLOGY_UNVERIFIED",
            "POSITION_STOP_TOPOLOGY_OBSERVED_NONAUTHORIZING",
        }:
            aggregate["status"] = mismatch["status"]
            for key in (
                "position_count", "current_tpsl_row_count",
                "positions_without_any_current_tpsl",
                "positions_without_positive_stop_loss_row",
                "positions_with_multiple_current_tpsl_rows",
                "current_tpsl_rows_with_unmatched_position",
                "current_tpsl_rows_without_position_id",
                "current_tpsl_rows_without_positive_stop_loss",
                "current_tpsl_rows_with_positive_stop_loss",
                "duplicate_current_tpsl_order_ids",
            ):
                value = mismatch.get(key)
                aggregate[key] = (
                    value if type(value) is int and 0 <= value <= 2000
                    else None
                )
            aggregate["position_ids_ambiguous"] = (
                mismatch.get("position_ids_ambiguous") is not False
            )
        else:
            aggregate = {"status": "POSITION_STOP_TOPOLOGY_UNVERIFIED"}
        aggregate.update({
            "native_stop_coverage_certified": False,
            "entry_allowed": False,
            "live_authorization": False,
        })
        result["stop_count_diagnostic"] = aggregate
    return result


class _PrivateGetOnly:
    """Expose only the signed GET transport, no mutation interface."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _private_get(
        self, path: str, params: dict[str, Any] | None = None,
    ) -> Any:
        return await self._client._private_get(path, params)


async def run_readonly_four_slot_audit(
    *, ledger: Any, client: Any, env: Mapping[str, str],
    shared_manual_observation: bool = False,
) -> dict[str, Any]:
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return _report("FOUR_SLOT_READONLY_PAPER_KILL_REQUIRED")
    try:
        result = await asyncio.wait_for(
            inspect_existing_four_slots_get_only(
                ledger=ledger, client=_PrivateGetOnly(client),
                shared_manual_observation=shared_manual_observation,
            ),
            timeout=75.0,
        )
    except asyncio.TimeoutError:
        return _report("FOUR_SLOT_READONLY_GET_OR_DB_TIMEOUT")
    except Exception:
        return _report("FOUR_SLOT_READONLY_OBSERVATION_FAILED")

    # Only allowlisted, predefined status fields, no account identifiers.
    allowed = {
        "FLAT_OBSERVED_NONAUTHORIZING",
        "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        "UNOWNED_OR_MISSING_CHECKPOINTS",
        "ACCOUNT_EXCEEDS_FOUR_SLOTS",
        "LEDGER_READ_OR_CONTRACT_FAILED",
        "INVENTORY_READ_OR_FORMAT_FAILED",
        "RECOVERY_BOOKEND_READ_FAILED",
        "CHECKPOINT_IDENTITY_INVALID",
        "LEDGER_CHANGED_DURING_OBSERVATION",
        "VENUE_CRITICAL_INVENTORY_CHANGED",
        "INVENTORY_COMPLETENESS_UNVERIFIED",
        "POSITION_STOP_COUNT_MISMATCH",
        "FLAT_HISTORY_REQUIRES_CLOSED_LEDGER_AUDIT",
        "ORIGIN_ORDER_UNAVAILABLE",
        "DURABLE_EXCHANGE_PROVENANCE_MISMATCH",
        "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
        "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        "SHARED_ACCOUNT_POSITION_PARTITION_UNVERIFIED",
        "SHARED_ACCOUNT_T100_LEDGER_HISTORY_UNVERIFIED",
        "SHARED_ACCOUNT_T100_DURABLE_PROVENANCE_UNVERIFIED",
        "SHARED_MODE_INPUT_INVALID",
    }
    status = result.status if result.status in allowed else "FOUR_SLOT_READONLY_UNKNOWN_STATE"
    owned = (
        result.occupied_slots if status in {
            "FLAT_OBSERVED_NONAUTHORIZING",
            "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
            "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        } else None
    )
    return _report(
        status, slots=owned, mismatch=result.mismatch_diagnostic,
        manual_positions=result.manual_positions_observed,
        unprotected_manual_positions=(
            result.manual_positions_without_positive_sl_observed
        ),
        account_positions=result.account_positions_observed,
    )


async def _main(
    env: Mapping[str, str], *,
    shared_manual_observation: bool = False,
) -> dict[str, Any]:
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return _report("FOUR_SLOT_READONLY_PAPER_KILL_REQUIRED")
    key, secret = env.get("MEXC_API_KEY"), env.get("MEXC_API_SECRET")
    if not key or not secret:
        return _report("FOUR_SLOT_READONLY_CREDENTIALS_MISSING")

    db_password = getpass.getpass(
        "Existing restricted T100 runtime DB password (hidden): "
    )
    dsn = getpass.getpass(
        "Existing Render PostgreSQL External URL (hidden): "
    )
    if not db_password or not dsn:
        return _report("FOUR_SLOT_READONLY_DB_CREDENTIALS_MISSING")

    from app.t100_live_role_preflight import TARGET_ROLE
    from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
    import asyncpg
    from app.mexc_trade import MexcTradeClient

    pool = None
    mexc = None
    try:
        pool = await asyncpg.create_pool(
            dsn=dsn, user=TARGET_ROLE, password=db_password,
            ssl="require", timeout=10, command_timeout=12,
            min_size=1, max_size=1,
            server_settings={"default_transaction_read_only": "on"},
        )
        mexc = MexcTradeClient(
            "https://api.mexc.com", api_key=key, api_secret=secret,
        )
        return await run_readonly_four_slot_audit(
            ledger=PostgresFourSlotLedgerReader(pool), client=mexc, env=env,
            shared_manual_observation=shared_manual_observation,
        )
    except Exception:
        return _report("FOUR_SLOT_READONLY_CONNECTION_OR_PRECONDITION_FAILED")
    finally:
        if mexc is not None:
            await mexc.close()
        if pool is not None:
            await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shared-manual-observation", action="store_true",
        help="GET/SELECT-only account partition with independent manual "
             "positions; NEVER authorizes cross-margin trading",
    )
    args = parser.parse_args()
    try:
        output = asyncio.run(_main(
            os.environ,
            shared_manual_observation=args.shared_manual_observation,
        ))
    except Exception:
        output = _report("FOUR_SLOT_READONLY_DIAGNOSTIC_HALTED")
    print(json.dumps(output, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
