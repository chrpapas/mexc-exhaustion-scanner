"""Manual, opt-in, GET-only MEXC cross-risk diagnostic for the operator.

No imports of trading/HTTP dependencies until paper-mode and kill guards pass.
No Render worker wiring, no DB access and no MEXC POST. A read-only API key
is recommended, but this module only guarantees its OWN code calls GET.
Even a successful run does not authorize orders or certify liquidation safety.

Run in the existing trusted Mac shell, with locally configured credentials:
T100_EXECUTION_MODE=paper T100_LIVE_ORDER_API_ENABLED=false \
T100_LIVE_KILL_SWITCH=true python3 -m app.t100_live_cross_risk_diagnostic

Never paste credentials. Output deliberately excludes symbols, IDs, balances,
prices, timestamps, leverage and exchange payloads.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Callable, Mapping

from app.t100_live_cross_risk_collector import MexcReadOnlyCrossRiskCollector
from app.t100_live_cross_risk_inventory_shape import (
    InventoryShapeObservation, observe_inventory_shape,
)
from app.t100_live_cross_risk_observation import (
    SAFE_COLLECTOR_FAILURE_CODES, SAFE_FRAME_FAILURE_CODES,
    SAFE_FAILURE_STAGES, observe_cross_risk_window,
)
from app.t100_live_guards import T100ExecutionControls
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_pnl_pair_recheck import (
    PnlPairRecheck, recheck_pnl_pair_readonly,
)


class CrossRiskDiagnosticHalted(RuntimeError):
    pass


def require_readonly_controls(env: Mapping[str, str]) -> None:
    """No HTTP/credential reading until all three explicit controls pass."""
    # Require an affirmative operator invocation, not permissive defaults.
    if (
        env.get("T100_EXECUTION_MODE", "").strip().lower() != "paper"
        or env.get("T100_LIVE_ORDER_API_ENABLED", "").strip().lower()
        not in {"false", "0", "off", "no"}
        or env.get("T100_LIVE_KILL_SWITCH", "").strip().lower()
        not in {"true", "1", "on", "yes"}
    ):
        raise CrossRiskDiagnosticHalted("RISK_DIAGNOSTIC_PAPER_KILL_REQUIRED")
    try:
        c = T100ExecutionControls.from_env(env)
    except (TypeError, ValueError):
        raise CrossRiskDiagnosticHalted("RISK_DIAGNOSTIC_CONTROLS_INVALID") from None
    if (
        c.mode != "paper"
        or c.live_orders_enabled
        or not c.kill_switch_engaged
    ):
        raise CrossRiskDiagnosticHalted("RISK_DIAGNOSTIC_PAPER_KILL_REQUIRED")


def _result(
    *, status: str, positions: int | None = None,
    witness: Any = None,
    inventory_shape: InventoryShapeObservation | None = None,
    pnl_recheck: PnlPairRecheck | None = None,
    original_pnl_failure_code: str = "NONE",
    full_window_retry_status: str = "NOT_CHECKED",
    full_window_retry_failure_code: str = "NONE",
) -> dict[str, Any]:
    """Allowlist-only output; never serialize a venue payload."""
    possible = {
        "CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING",
        "CROSS_RISK_WINDOW_CONFIG_INVALID",
        "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED",
        "CROSS_RISK_READ_WINDOW_NOT_CONSECUTIVE",
        "CROSS_RISK_CRITICAL_STATE_DRIFT",
        "CROSS_RISK_FAIR_PRICE_DRIFT",
        "CROSS_RISK_FAIR_PRICE_DRIFT_UNVERIFIED",
        "CROSS_RISK_WINDOW_DRIFT_LIMIT_INVALID",
        "RISK_DIAGNOSTIC_CONTROLS_INVALID",
        "RISK_DIAGNOSTIC_PAPER_KILL_REQUIRED",
        "RISK_DIAGNOSTIC_CREDENTIALS_MISSING",
        "RISK_DIAGNOSTIC_GET_TIMED_OUT",
        "RISK_DIAGNOSTIC_GET_FAILED",
    }
    safe_status = status if status in possible else "RISK_DIAGNOSTIC_GET_FAILED"
    # A blocked risk witness may use zero as its DEFAULT count. Only a
    # separate stable two-read inventory observation may report a flat
    # account or raw counts on a blocked risk assessment.
    complete = safe_status == "CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING"
    n = positions if complete and type(positions) is int and 1 <= positions <= 4 else None
    shape = inventory_shape if isinstance(inventory_shape, InventoryShapeObservation) else None
    shape_stable = shape is not None and shape.stable_across_reads is True
    if not complete and shape_stable and type(shape.observed_positions) is int:
        n = shape.observed_positions
    stops = (
        witness.observed_current_stops
        if complete and type(getattr(witness, "observed_current_stops", None)) is int
        else shape.observed_current_stops if shape_stable else None
    )
    stop_reported = shape.reported_stop_orders if shape_stable else None
    order_total = shape.reported_total_open_orders if shape_stable else None
    shape_status = shape.status if shape is not None else "NOT_CHECKED"
    allowed_shape_statuses = {
        "INVENTORY_SHAPE_PROBE_INVALID",
        "INVENTORY_SHAPE_ACCOUNT_STATE_DRIFT",
        "INVENTORY_SHAPE_STOP_COUNT_DISAGREES_WITH_EXCHANGE",
        "INVENTORY_SHAPE_FLAT_AND_ORDER_FREE_OBSERVED_NONATOMIC",
        "INVENTORY_SHAPE_FLAT_WITH_OPEN_ORDERS",
        "INVENTORY_SHAPE_STOPS_WITHOUT_POSITION",
        "INVENTORY_SHAPE_MORE_THAN_FOUR_POSITIONS",
        "INVENTORY_SHAPE_NONFLAT_STOP_ROW_COUNT_DIFFERS",
        "INVENTORY_SHAPE_NONFLAT_COUNTS_MATCH_NONAUTHORIZING",
        "INVENTORY_SHAPE_READ_UNVERIFIED",
        "NOT_CHECKED",
    }
    shape_status = shape_status if shape_status in allowed_shape_statuses else "NOT_CHECKED"
    stage = getattr(witness, "failure_stage", "NONE")
    stage = stage if type(stage) is str and stage in SAFE_FAILURE_STAGES else "NONE"
    code = getattr(witness, "failure_code", "NONE")
    safe_codes = SAFE_COLLECTOR_FAILURE_CODES | SAFE_FRAME_FAILURE_CODES | {
        "NONE", "UNCLASSIFIED_READ_OR_EVIDENCE_FAILURE",
    }
    code = code if isinstance(code, str) and code in safe_codes else "NONE"
    # Allowlisted public contract schema only. Not two-frame-certified
    # evidence, not private risk limits and never permission to trade.
    def safe_shape_field(attr: str, values: set[str]) -> str:
        raw = getattr(witness, attr, "NOT_CHECKED")
        return raw if type(raw) is str and raw in values else "NOT_CHECKED"

    risk_mode = safe_shape_field(
        "risk_schedule_mode", {"CUSTOM", "INCREASE", "MIXED", "UNKNOWN", "NOT_CHECKED"},
    )
    risk_type = safe_shape_field(
        "risk_schedule_type", {"BY_VOLUME", "BY_VALUE", "MIXED", "UNKNOWN", "NOT_CHECKED"},
    )
    risk_fields = safe_shape_field("risk_schedule_fields", {
        "CUSTOM_FIELDS_PRESENT", "CUSTOM_FIELDS_MISSING_OR_MALFORMED",
        "INCREMENTAL_FIELDS_PRESENT_UNMODELED",
        "INCREMENTAL_FIELDS_MISSING_OR_MALFORMED",
        "INCREMENTAL_FIELDS_INCOHERENT", "UNVERIFIED", "MIXED",
        "NOT_CHECKED",
    })
    if code != "RISK_TIER_STATIC_VOLUME_SCHEDULE_UNAVAILABLE":
        risk_mode, risk_type, risk_fields = (
            "NOT_CHECKED", "NOT_CHECKED", "NOT_CHECKED",
        )
    pnl = pnl_recheck if type(pnl_recheck) is PnlPairRecheck else None
    return {
        "status": safe_status,
        "failure_stage": stage,
        "failure_code": code,
        "pnl_pair_recheck_status": (
            pnl.status if pnl is not None else "NOT_CHECKED"
        ),
        "initial_pnl_window_failure_code": (
            original_pnl_failure_code
            if original_pnl_failure_code in {
                "COLLECTOR_PRE_UNREALIZED_PNL_MISMATCH",
                "COLLECTOR_POST_UNREALIZED_PNL_MISMATCH",
            }
            else "NONE"
        ),
        "pnl_full_window_retry_status": (
            full_window_retry_status
            if full_window_retry_status in possible else "NOT_CHECKED"
        ),
        "pnl_full_window_retry_failure_code": (
            full_window_retry_failure_code
            if full_window_retry_failure_code
            in SAFE_COLLECTOR_FAILURE_CODES | SAFE_FRAME_FAILURE_CODES
            | {"NONE", "UNCLASSIFIED_READ_OR_EVIDENCE_FAILURE"}
            else "NONE"
        ),
        "pnl_pair_recheck_pairs_observed": (
            pnl.pairs_observed if pnl is not None else 0
        ),
        "pnl_pair_recheck_positions_stable": (
            pnl.positions_stable_across_pairs if pnl is not None else False
        ),
        "pnl_pair_recheck_position_pnl_moved": (
            pnl.position_unrealized_moved_across_pairs
            if pnl is not None else False
        ),
        "pnl_pair_recheck_asset_pnl_moved": (
            pnl.asset_unrealized_moved_across_pairs
            if pnl is not None else False
        ),
        "pnl_pair_recheck_first_pair_matched": (
            pnl.first_pair_reconciled if pnl is not None else False
        ),
        "pnl_pair_recheck_second_pair_matched": (
            pnl.second_pair_reconciled if pnl is not None else False
        ),
        "risk_schedule_mode": risk_mode,
        "risk_schedule_type": risk_type,
        "risk_schedule_fields": risk_fields,
        "risk_schedule_two_frame_certified": False,
        "observed_positions": n,
        "observed_current_stops": stops,
        "reported_stop_orders": stop_reported,
        "reported_total_open_orders": order_total,
        "inventory_shape_status": shape_status,
        "inventory_shape_stable": shape_stable,
        "native_stop_protection_verified": False,
        "read_only": True,
        "private_get_only": True,
        "mexc_orders_sent": False,
        "render_deployed": False,
        "entry_allowed": False,
        "live_authorization": False,
        "exchange_atomicity_verified": False,
        "account_wide_completeness_certified": False,
        "t100_position_ownership_certified": False,
        "cross_wallet_collateral_certified": False,
        "future_stressed_tiers_certified": False,
        "venue_liquidation_certified": False,
        "critical_inventory_stable": bool(
            getattr(witness, "stable_critical_inventory", False) is True
        ),
        "current_risk_tiers_observed": bool(
            getattr(witness, "current_risk_tiers_observed", False) is True
        ),
        "fair_prices_observed": bool(
            getattr(witness, "fair_prices_observed", False) is True
        ),
        "position_margin_economic_witness_observed": bool(
            getattr(witness, "position_margin_economic_witness_observed", False) is True
        ),
    }


async def run_diagnostic(
    *, private: ReadOnlyMexcProbe, public: Any,
    clock_ms: Callable[[], int],
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Dependency-injected diagnostics can be tested without HTTP or secrets."""
    if not (type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 60):
        return _result(status="RISK_DIAGNOSTIC_GET_FAILED")
    try:
        reader = MexcReadOnlyCrossRiskCollector(
            private=private, public=public,
            clock_ms=clock_ms, max_frame_ms=5000,
        )
        report = await asyncio.wait_for(
            observe_cross_risk_window(
                reader, now_ms=clock_ms,
                max_age_ms=5000,
                max_separation_ms=5000,
            ),
            timeout=timeout_seconds,
        )
        shape = None
        if (
            report.status == "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED"
            and report.failure_stage in {"READ_FIRST", "READ_SECOND"}
            and report.failure_code == "COLLECTOR_NONFLAT_ONE_TO_FOUR_REQUIRED"
        ):
            # The nonflat-only risk witness cannot assess 0 positions, >4
            # positions, or a different number of TP/SL rows. A separate
            # bounded GET-only diagnostic distinguishes those cases without
            # weakening the V40 risk/ownership gates.
            shape = await asyncio.wait_for(
                observe_inventory_shape(private),
                timeout=min(timeout_seconds, 15.0),
            )
        # A transient PNL mismatch is investigated with two strict paired
        # GETs, then ONE complete new two-frame cross-risk window. Never
        # invent a tolerance or reuse the original inconsistent snapshot.
        # A later successfully checked window is fresh, but NONAUTHORIZING.
        pnl_recheck = None
        original_pnl_code = "NONE"
        full_retry_status = "NOT_CHECKED"
        full_retry_code = "NONE"
        if (
            report.status == "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED"
            and report.failure_stage in {"READ_FIRST", "READ_SECOND"}
            and report.failure_code in {
                "COLLECTOR_PRE_UNREALIZED_PNL_MISMATCH",
                "COLLECTOR_POST_UNREALIZED_PNL_MISMATCH",
            }
        ):
            try:
                pnl_recheck = await asyncio.wait_for(
                    recheck_pnl_pair_readonly(private),
                    timeout=min(timeout_seconds, 8.0),
                )
            except Exception:
                pnl_recheck = PnlPairRecheck(
                    status="PNL_RECHECK_READ_OR_SCHEMA_UNVERIFIED",
                )
        if (
            pnl_recheck is not None
            and pnl_recheck.status == "PNL_RECHECK_TWO_PAIRS_MATCH_NONAUTHORIZING"
            and pnl_recheck.positions_stable_across_pairs is True
            and pnl_recheck.position_unrealized_moved_across_pairs is False
            and pnl_recheck.asset_unrealized_moved_across_pairs is False
        ):
            original_pnl_code = report.failure_code
            try:
                fresh = await asyncio.wait_for(
                    observe_cross_risk_window(
                        reader, now_ms=clock_ms,
                        max_age_ms=5000, max_separation_ms=5000,
                    ),
                    timeout=min(timeout_seconds, 15.0),
                )
                full_retry_status = fresh.status
                full_retry_code = fresh.failure_code
                # The fresh window is a wholly independent strict witness.
                # Preserve its own failure state if the next gate blocks.
                report = fresh
            except Exception:
                full_retry_status = "RISK_DIAGNOSTIC_GET_FAILED"
                full_retry_code = "NONE"
                # Keep the original failure if the follow-up could not run.
        return _result(
            status=report.status,
            positions=report.observed_positions,
            witness=report,
            inventory_shape=shape,
            pnl_recheck=pnl_recheck,
            original_pnl_failure_code=original_pnl_code,
            full_window_retry_status=full_retry_status,
            full_window_retry_failure_code=full_retry_code,
        )
    except asyncio.TimeoutError:
        return _result(status="RISK_DIAGNOSTIC_GET_TIMED_OUT")
    except Exception:
        # Never expose URL, account identifiers, exception messages,
        # hidden endpoint data, or raw venue payloads.
        return _result(status="RISK_DIAGNOSTIC_GET_FAILED")


async def _main(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted as exc:
        return _result(status=str(exc))
    key, secret = env.get("MEXC_API_KEY", ""), env.get("MEXC_API_SECRET", "")
    if not key or not secret:
        return _result(status="RISK_DIAGNOSTIC_CREDENTIALS_MISSING")

    # Import actual network client only AFTER preflight; no mutation methods
    # are invoked or passed into the read-only collector.
    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        probe = ReadOnlyMexcProbe(client)
        return await run_diagnostic(
            private=probe, public=client.client,
            clock_ms=lambda: int(time.time() * 1000),
        )
    except Exception:
        return _result(status="RISK_DIAGNOSTIC_GET_FAILED")
    finally:
        await client.close()


def cli() -> None:
    try:
        result = asyncio.run(_main())
    except Exception:
        result = _result(status="RISK_DIAGNOSTIC_GET_FAILED")
    print(json.dumps(result, sort_keys=True, indent=2))
    print("PRIVATE/PUBLIC GET ONLY; NO ORDERS; LIVE TRADING DISABLED.")
    if result["status"] != "CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING":
        raise SystemExit(2)


if __name__ == "__main__":
    cli()
