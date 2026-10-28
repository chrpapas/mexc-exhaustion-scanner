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
from app.t100_live_cross_risk_observation import (
    SAFE_COLLECTOR_FAILURE_CODES, SAFE_FRAME_FAILURE_CODES,
    SAFE_FAILURE_STAGES, observe_cross_risk_window,
)
from app.t100_live_guards import T100ExecutionControls
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


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
    # A blocked witness may use zero as its DEFAULT count. Do not represent
    # that as an actual flat-account observation.
    complete = safe_status == "CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING"
    n = positions if complete and type(positions) is int and 1 <= positions <= 4 else None
    stage = getattr(witness, "failure_stage", "NONE")
    stage = stage if type(stage) is str and stage in SAFE_FAILURE_STAGES else "NONE"
    code = getattr(witness, "failure_code", "NONE")
    safe_codes = SAFE_COLLECTOR_FAILURE_CODES | SAFE_FRAME_FAILURE_CODES | {
        "NONE", "UNCLASSIFIED_READ_OR_EVIDENCE_FAILURE",
    }
    code = code if isinstance(code, str) and code in safe_codes else "NONE"
    return {
        "status": safe_status,
        "failure_stage": stage,
        "failure_code": code,
        "observed_positions": n,
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
        return _result(
            status=report.status,
            positions=report.observed_positions,
            witness=report,
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
