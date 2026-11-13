"""Separate-key V40 guardian: standby FLAT or one verified CROSS SHORT.

Uses the exact immutable V40 checkpoint and its matched entry order to
distinguish a legitimate managed SHORT from an unrelated/manual position.
The guardian is READ ONLY for both MEXC and Postgres. A position with no
durable checkpoint, pending SUBMITTING claim, missing native MARKET SL,
bad leverage or unverified fills is a CRITICAL incident, never adopted or
auto-closed.

No API key fingerprint/enrollment table is used. The dedicated observer
credential must be scoped to the same isolated V40 subaccount; that account
relationship must be checked out of band by the operator before deployment.
"""
from __future__ import annotations

import asyncio
import ssl
from typing import Any,Mapping

from app.t100_live_four_slot_mexc_readonly import _components
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,
)
from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport
from app.t100_v40_canary_account_guardian import (
    V40CanaryGuardianHeartbeat,verify_guardian_environment,
)
from app.t100_v40_guardian_ledger_reader import (
    PostgresV40GuardianLedgerReader,
)
from app.t100_v40_guardian_owned_cross import (
    assess_v40_owned_cross_guardian,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader


# Finite, internal coordinator statuses only. Never include a raw exception,
# exchange JSON field, SQL value, account identifier, or arbitrary status.
_SAFE_INVENTORY_CERT_FAILURES=frozenset({
    "CURRENT_INVENTORY_COMPLETENESS_PROBE_HALTED",
    "CURRENT_INVENTORY_COMPLETENESS_INPUT_AMBIGUOUS",
    "CURRENT_TPSL_COUNT_CONTRACT_INVALID",
    "CURRENT_INVENTORY_NOT_STABLE",
    "POSITION_MARGIN_WITNESS_MISSING",
    "CURRENT_TPSL_COUNT_MISMATCH",
    "SYMBOL_SCOPED_REDUNDANCY_MISMATCH",
    "CURRENT_INVENTORY_CONTRACT_INCOMPLETE",
    "CURRENT_ACCOUNT_INVENTORY_COMPLETENESS_VERIFIED_NONATOMIC",
})

_SAFE_SNAPSHOT_NONREADY=frozenset({
    "SHARED_MODE_INPUT_INVALID",
    "LEDGER_READ_OR_CONTRACT_FAILED",
    "ACCOUNT_EXCEEDS_FOUR_SLOTS",
    "INVENTORY_READ_OR_FORMAT_FAILED",
    "ORIGIN_ORDER_UNAVAILABLE",
    "CHECKPOINT_IDENTITY_INVALID",
    "RECOVERY_BOOKEND_READ_FAILED",
    "LEDGER_CHANGED_DURING_OBSERVATION",
    "VENUE_CRITICAL_INVENTORY_CHANGED",
    "INVENTORY_COMPLETENESS_UNVERIFIED",
    "SHARED_ACCOUNT_POSITION_PARTITION_UNVERIFIED",
    "SHARED_ACCOUNT_T100_LEDGER_HISTORY_UNVERIFIED",
    "SHARED_ACCOUNT_T100_DURABLE_PROVENANCE_UNVERIFIED",
    "POSITION_STOP_COUNT_MISMATCH",
    "UNOWNED_OR_MISSING_CHECKPOINTS",
    "FLAT_HISTORY_REQUIRES_CLOSED_LEDGER_AUDIT",
    "DURABLE_EXCHANGE_PROVENANCE_MISMATCH",
})


def _halt(code:str)->V40CanaryGuardianHeartbeat:
    return V40CanaryGuardianHeartbeat(status=code)


async def observe_v40_account_and_owned(
    *,ledger:PostgresV40GuardianLedgerReader|PostgresFourSlotLedgerReader,
    signed_get:GetOnlyMexcTransport,
) -> V40CanaryGuardianHeartbeat:
    """Independent full-account inventory/ledger bookends, no mutation."""
    if (
        type(ledger) not in (
            PostgresV40GuardianLedgerReader,PostgresFourSlotLedgerReader,
        )
        or type(signed_get) is not GetOnlyMexcTransport
        or hasattr(signed_get,"_private_post")
    ):
        return _halt("V40_GUARDIAN_INDEPENDENT_READER_OR_GET_INVALID_HALT")
    venue,completeness=_components(signed_get)
    try:
        # The exchange hedge mode is a separate signed GET; position
        # openType=CROSS alone cannot attest the account's position mode.
        mode=await ReadOnlyMexcProbe(signed_get).raw_position_mode()
        if mode!=1:
            return _halt("V40_GUARDIAN_ACCOUNT_HEDGE_MODE_UNVERIFIED_HALT")
        snapshot=await FourSlotSnapshotCoordinator(
            ledger=ledger,venue=venue,completeness=completeness,
        ).inspect()
    except Exception:
        return _halt("V40_GUARDIAN_ACCOUNT_LEDGER_OR_GET_UNAVAILABLE_HALT")
    if (
        snapshot.status=="FLAT_OBSERVED_NONAUTHORIZING"
        and snapshot.occupied_slots==0
        and snapshot.inventory is not None
        and snapshot.inventory.get("account_wide_completeness_verified") is True
        and snapshot.inventory.get(
            "critical_inventory_equal_across_witness_window"
        ) is True
    ):
        return V40CanaryGuardianHeartbeat(
            "V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING",
            monitored_account_flat=True,
            inventory_two_signed_get_bookends=True,
            hedge_mode_verified=True,
            incident_alert=False,
            ledger_provenance_verified=True,
        )
    if (
        snapshot.status!="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
        or snapshot.occupied_slots!=1
    ):
        if (
            snapshot.status=="INVENTORY_COMPLETENESS_UNVERIFIED"
            and type(snapshot.inventory) is dict
            and snapshot.inventory.get("status") in _SAFE_INVENTORY_CERT_FAILURES
        ):
            # Only exact finite certificate codes; no MEXC response bodies.
            return _halt(
                "V40_GUARDIAN_INVENTORY_"+
                snapshot.inventory["status"]+"_HALT"
            )
        return _halt(
            "V40_GUARDIAN_SNAPSHOT_"+snapshot.status+"_HALT"
            if snapshot.status in _SAFE_SNAPSHOT_NONREADY else
            "V40_GUARDIAN_UNOWNED_OR_UNKNOWN_PENDING_MUTATION_HALT"
        )
    try:
        second_ledger=await ledger.read_snapshot()
        audited=assess_v40_owned_cross_guardian(
            snapshot=snapshot,ledger=second_ledger,
            positions=venue.last_positions,
            stops=venue.last_stops,
        )
    except Exception:
        return _halt("V40_GUARDIAN_OWNED_CROSS_AUDIT_FAILED_HALT")
    if (
        audited.status!=
            "V40_ONE_OWNED_CROSS_SHORT_NATIVE_MARKET_SL_OBSERVED_NONAUTHORIZING"
        or audited.incident_alert is not False
        or audited.native_market_sl_exact_order_and_quantity_verified is not True
        or audited.single_durable_owned_short is not True
        or audited.cross_1x_position_verified is not True
        or audited.account_wide_completeness_observed is not True
    ):
        return _halt("V40_GUARDIAN_OWNED_CROSS_OR_NATIVE_SL_UNVERIFIED_HALT")
    return V40CanaryGuardianHeartbeat(
        "V40_GUARDIAN_ONE_OWNED_CROSS_SHORT_NATIVE_SL_OBSERVED_NONAUTHORIZING",
        monitored_account_flat=False,
        inventory_two_signed_get_bookends=True,
        hedge_mode_verified=True,
        incident_alert=False,
        ledger_provenance_verified=True,
        single_owned_cross_native_sl_verified=True,
    )


async def observe_v40_account_and_owned_network(
    env:Mapping[str,str],
) -> V40CanaryGuardianHeartbeat:
    """Production mandatory distinct read-only Postgres role and GET-only key."""
    try:
        verify_guardian_environment(env)
    except Exception:
        return _halt("V40_GUARDIAN_INDEPENDENT_CREDENTIAL_SCOPE_HALT")
    shared=env.get("T100_V40_GUARDIAN_REUSE_V40_RUNTIME_DB")=="true"
    dsn=env.get(
        "T100_V40_GUARDIAN_SHARED_DB_DSN" if shared
        else "T100_V40_GUARDIAN_LEDGER_READONLY_DSN",""
    )
    cafile=env.get("T100_V40_GUARDIAN_LEDGER_CA_FILE","")
    if (
        type(dsn) is not str or not dsn.startswith(
            ("postgresql://","postgres://")
        )
        or (
            not shared and
            (type(cafile) is not str or not cafile.startswith("/"))
        )
        or (
            not shared and dsn in (
                env.get("T100_V40_EXECUTOR_RUNTIME_DSN"),
                env.get("T100_V40_SIGNAL_READER_DSN"),
            )
        )
        or (
            shared and dsn==env.get("T100_V40_SIGNAL_READER_DSN")
            and dsn
        )
    ):
        return _halt("V40_GUARDIAN_DISTINCT_LEDGER_READER_NOT_PROVISIONED_HALT")
    import asyncpg
    import httpx
    pool=None
    try:
        if shared:
            # Actual runtime role identity and a READ ONLY repeatable-read
            # snapshot are independently rechecked on EVERY journal query.
            context="require"
            reader_class=PostgresFourSlotLedgerReader
        else:
            context=ssl.create_default_context(cafile=cafile)
            context.check_hostname=True
            context.verify_mode=ssl.CERT_REQUIRED
            reader_class=PostgresV40GuardianLedgerReader
        pool=await asyncpg.create_pool(
            dsn=dsn,ssl=context,min_size=1,max_size=1,
            timeout=9,command_timeout=12,
            server_settings={"default_transaction_read_only":"on"},
        )
        async with httpx.AsyncClient(
            base_url=GetOnlyMexcTransport.BASE_URL,
            trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(8.0),
        ) as client:
            get_only=GetOnlyMexcTransport(
                client,
                api_key=env["T100_V40_GUARDIAN_READONLY_API_KEY"],
                api_secret=env["T100_V40_GUARDIAN_READONLY_API_SECRET"],
            )
            return await asyncio.wait_for(
                observe_v40_account_and_owned(
                    ledger=reader_class(pool),
                    signed_get=get_only,
                ),timeout=45,
            )
    except Exception:
        return _halt("V40_GUARDIAN_INDEPENDENT_DB_OR_VENUE_GET_FAILED_HALT")
    finally:
        if pool is not None:
            await pool.close()
