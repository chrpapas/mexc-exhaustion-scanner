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
from app.t100_v40_four_slot_guardian_policy import (
    audit_frozen_four_slot_native_stops,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe, VenueReadHalted
from app.t100_v40_guardian_get_failure_codes import guardian_mode_halt_from_venue_error
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader


# Finite, internal coordinator statuses only. Never include a raw exception,
# exchange JSON field, SQL value, account identifier, or arbitrary status.
_SAFE_INVENTORY_PROBE_STAGES=frozenset({
    "BEFORE_POSITIONS","BEFORE_STOPS","SYMBOL_CROSSCHECK",
    "ACCOUNT_ASSETS","POSITION_MARGIN","OPEN_ORDER_COUNTS",
    "AFTER_POSITIONS","AFTER_STOPS","CERTIFICATE",
})

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


# GET operations are idempotent. This bounded retry can recover ONE
# transient position-mode-only network/5xx/rate-limit failure. It NEVER
# recovers auth/permissions, a wrong hedge mode, inventory drift, missing
# native protection, or a latched critical incident. Crucially, a successful
# retry is followed by the ordinary independent two-bookend account+ledger
# inspection; a recovered mode GET alone proves NOTHING about safe inventory.
_TRANSIENT_MODE_GET_REASONS=frozenset({
    "POSITION_MODE_HTTP_429","POSITION_MODE_HTTP_5XX",
    "POSITION_MODE_TIMEOUT","POSITION_MODE_NETWORK",
    "POSITION_MODE_API_RATE_LIMIT","POSITION_MODE_API_SERVER",
})


async def _hedge_mode_bounded_get(
    signed_get:GetOnlyMexcTransport,
    *,
    retry_transient:bool=False,
    pause:Any=asyncio.sleep,
)->int:
    if type(retry_transient) is not bool or not callable(pause):
        raise VenueReadHalted("V40_GUARDIAN_HEDGE_MODE_RETRY_POLICY_INVALID")
    probe=ReadOnlyMexcProbe(signed_get)
    try:
        return await probe.raw_position_mode()
    except VenueReadHalted as error:
        code=error.args[0] if len(error.args)==1 else None
        if (
            not retry_transient or type(code) is not str
            or code not in _TRANSIENT_MODE_GET_REASONS
        ):
            raise
        # MEXC HTTP 429/API rate limit warrants a slower probe than network
        # jitter. No POST, SQL mutation, order retry, or latch update here.
        await pause(3.0 if "RATE_LIMIT" in code or "429" in code else 1.0)
        # Only one retry, never a loop. The caller still halts on any failure.
        return await probe.raw_position_mode()


async def observe_v40_account_and_owned(
    *,ledger:PostgresV40GuardianLedgerReader|PostgresFourSlotLedgerReader,
    signed_get:GetOnlyMexcTransport,
    retry_transient_mode_get:bool=False,
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
    try:
        venue,completeness=_components(signed_get)
    except Exception:
        return _halt("V40_GUARDIAN_READONLY_COMPONENTS_INIT_FAILED_HALT")
    # Independent hedge-mode GET: a transient venue error is NOT evidence
    # of a flat account. Keep fail-closed behavior while identifying stage.
    try:
        mode=await _hedge_mode_bounded_get(
            signed_get,retry_transient=retry_transient_mode_get,
        )
    except VenueReadHalted as error:
        # Diagnostic classification never permits entry or weakens the latch.
        return _halt(guardian_mode_halt_from_venue_error(error))
    except Exception:
        return _halt("V40_GUARDIAN_HEDGE_MODE_SIGNED_GET_FAILED_HALT")
    if mode!=1:
        return _halt("V40_GUARDIAN_ACCOUNT_HEDGE_MODE_UNVERIFIED_HALT")
    try:
        snapshot=await FourSlotSnapshotCoordinator(
            ledger=ledger,venue=venue,completeness=completeness,
        ).inspect()
    except Exception:
        return _halt("V40_GUARDIAN_SNAPSHOT_RUNTIME_EXCEPTION_HALT")
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
        or not 1<=snapshot.occupied_slots<=4
    ):
        from app.t100_v40_guardian_get_failure_codes import (
            guardian_inventory_diagnostic_halt,
        )
        if (
            snapshot.status=="INVENTORY_READ_OR_FORMAT_FAILED"
            and type(snapshot.mismatch_diagnostic) is dict
        ):
            coded=guardian_inventory_diagnostic_halt(
                snapshot.mismatch_diagnostic.get("stage"),
                snapshot.mismatch_diagnostic.get("category"),
            )
            if coded is not None:
                return _halt(coded)
        if (
            snapshot.status=="INVENTORY_COMPLETENESS_UNVERIFIED"
            and type(snapshot.inventory) is dict
            and snapshot.inventory.get("status") in _SAFE_INVENTORY_CERT_FAILURES
        ):
            # Only exact finite certificate codes/stages; no response bodies.
            if (
                snapshot.inventory["status"]==
                    "CURRENT_INVENTORY_COMPLETENESS_PROBE_HALTED"
                and snapshot.inventory.get(
                    "failure_stage"
                ) in _SAFE_INVENTORY_PROBE_STAGES
            ):
                coded=guardian_inventory_diagnostic_halt(
                    snapshot.inventory.get("failure_stage"),
                    snapshot.inventory.get("failure_category"),
                )
                if coded is not None:
                    return _halt(coded)
                return _halt(
                    "V40_GUARDIAN_INVENTORY_STAGE_"+
                    snapshot.inventory["failure_stage"]+"_HALT"
                )
            return _halt(
                "V40_GUARDIAN_INVENTORY_"+
                snapshot.inventory["status"]+"_HALT"
            )
        return _halt(
            "V40_GUARDIAN_SNAPSHOT_"+snapshot.status+"_HALT"
            if snapshot.status in _SAFE_SNAPSHOT_NONREADY else
            "V40_GUARDIAN_UNOWNED_OR_UNKNOWN_PENDING_MUTATION_HALT"
        )
    # Unreleased historical 1-4 slot provenance tests are now wired into
    # the independent live guardian witness. For 2-4 SHORT positions every
    # matched native MARKET SL and every durable checkpoint must survive the
    # common exchange/ledger bookend audit. This is not a live trade permit.
    if 2<=snapshot.occupied_slots<=4:
        try:
            second_ledger=await ledger.read_snapshot()
            audited_four=audit_frozen_four_slot_native_stops(
                snapshot=snapshot,ledger=second_ledger,
                account_positions=venue.last_positions,
                account_stops=venue.last_stops,
            )
        except Exception:
            return _halt("V40_GUARDIAN_MULTI_OWNED_AUDIT_RUNTIME_HALT")
        if not (
            audited_four.status==
                "V40_FOUR_SLOT_ACCOUNT_NATIVE_MARKET_SL_SIGNED_GET_NONAUTHORIZING"
            and audited_four.protected_owned_positions==snapshot.occupied_slots
            and audited_four.matching_native_market_stops==snapshot.occupied_slots
            and audited_four.separate_symbols_verified is True
            and audited_four.account_bookends_verified is True
            and audited_four.ledger_bookends_verified is True
            and audited_four.all_owned_cross_positions_verified is True
            and audited_four.incident_alert is False
            and audited_four.live_authorization is False
            and audited_four.mexc_posts==audited_four.database_writes==0
        ):
            return _halt("V40_GUARDIAN_MULTI_OWNED_CROSS_OR_NATIVE_SL_UNVERIFIED_HALT")
        return V40CanaryGuardianHeartbeat(
            "V40_GUARDIAN_TWO_TO_FOUR_OWNED_CROSS_NATIVE_SL_OBSERVED_NONAUTHORIZING",
            monitored_account_flat=False,
            inventory_two_signed_get_bookends=True,
            hedge_mode_verified=True,
            ledger_provenance_verified=True,
            incident_alert=False,
            owned_cross_positions_count=snapshot.occupied_slots,
            all_owned_cross_native_market_sl_verified=True,
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
        owned_cross_positions_count=1,
        all_owned_cross_native_market_sl_verified=True,
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
                diagnostic_get_errors=True,
            )
            return await asyncio.wait_for(
                observe_v40_account_and_owned(
                    ledger=reader_class(pool),
                    signed_get=get_only,
                    retry_transient_mode_get=(
                        env.get("T100_V40_GUARDIAN_TRANSIENT_MODE_RETRY_ENABLED")
                        =="true"
                    ),
                ),timeout=45,
            )
    except Exception:
        return _halt("V40_GUARDIAN_INDEPENDENT_DB_OR_VENUE_GET_FAILED_HALT")
    finally:
        if pool is not None:
            await pool.close()
