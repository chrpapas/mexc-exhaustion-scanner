"""Read-only, exchange-observation-led readiness evidence for T100 WEB pilot.

Combines exact FOURTH and FIFTH pilot signed-GET reports from the dedicated
MEXC web subaccount. Prioritizes verified venue behavior over uncertain
documentation, while never converting conflicting provenance into an SL
certification, weakening production authorization, or contacting BOXWAVE.

The independent T100 main trader/transport remains PAPER ONLY and must
have its own safety, strategy, recovery and accounting release evidence.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from typing import Any

from app import t100_web_pilot_fourth_close_audit_cli as fourth
from app import t100_web_pilot_fifth_native_exit_audit_cli as fifth
from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app.t100_live_current_inventory_cli import read_pilot_keychain_credentials
from app.t100_web_pilot_one_shot_cli import (
    OneShotProcessLock, _allow_real_mode_env,
)
from app.mexc_trade import MexcTradeClient

FOURTH_STATUS = (
    "FOURTH_EXACT_MANUAL_MARKET_CLOSE_AND_EDITED_STOP_CANCELED_VERIFIED"
    "_NONAUTHORIZING"
)
FIFTH_STATUS = (
    "FIFTH_NATIVE_ATTACHED_EXIT_EXACT_MARKET_FILL_VERIFIED"
    "_TP_CLASSIFICATION_CONFLICT_NONAUTHORIZING"
)


def evaluate_exchange_observations(
    fourth_result: dict[str, Any],
    fifth_result: dict[str, Any],
) -> dict[str, Any]:
    """Pure fail-closed categorization: observed facts vs unknown release gates."""
    fourth_ok = all((
        fourth_result.get("status") == FOURTH_STATUS,
        fourth_result.get("credential_scope") == "T100_WEB_PILOT_SUBACCOUNT",
        fourth_result.get("account_bookended_flat_verified") is True,
        fourth_result.get("fourth_entry_filled_verified") is True,
        fourth_result.get("fourth_amendment_ack_journaled") is True,
        fourth_result.get("manual_market_close_exactly_verified") is True,
        fourth_result.get("finished_stop_exact_identity_verified") is True,
        fourth_result.get("finished_stop_price_matches_amended_target") is True,
        fourth_result.get("finished_stop_canceled_not_triggered") is True,
        fourth_result.get("mexc_orders_sent") == 0,
        fourth_result.get("live_authorization") is False,
    ))
    fifth_ok = all((
        fifth_result.get("status") == FIFTH_STATUS,
        fifth_result.get("credential_scope") == "T100_WEB_PILOT_SUBACCOUNT",
        fifth_result.get("account_bookended_flat_verified") is True,
        fifth_result.get("entry_exact_filled") is True,
        fifth_result.get("one_attached_finished_stop_row") is True,
        fifth_result.get("stop_order_terminal_executed") is True,
        fifth_result.get("stop_price_matches_fifth_journal") is True,
        fifth_result.get("exchange_generated_close_id_present") is True,
        fifth_result.get("generated_close_exact_full_market_order") is True,
        fifth_result.get("generated_close_full_quantity_deals_verified") is True,
        fifth_result.get("historical_stop_error_code") == "ZERO",
        fifth_result.get("historical_trigger_side") == "TP",
        fifth_result.get("manual_close_attempted_in_fifth_journal") is False,
        fifth_result.get("native_attached_exit_execution_correlated") is True,
        fifth_result.get("native_sl_trigger_certified") is False,
        fifth_result.get("mexc_orders_sent") == 0,
        fifth_result.get("live_authorization") is False,
    ))
    no_tp_price_observed = (
        fifth_result.get("no_take_profit_price_observed") is True
    )
    live_amendment_observed = fourth_ok
    native_attached_exit_observed = fifth_ok
    corroborated_sl_like_exit = fifth_ok and no_tp_price_observed
    return {
        "status": (
            "FOURTH_AND_FIFTH_LIVE_VENUE_OBSERVATIONS_RECONCILED"
            "_NONAUTHORIZING"
            if fourth_ok and fifth_ok
            else "PILOT_EVIDENCE_INCOMPLETE_REVIEW_BEFORE_ADVANCING"
        ),
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "fourth_pilot_exact_manual_market_close_verified": fourth_ok,
        "one_supervised_native_stop_price_edit_observed": live_amendment_observed,
        "fifth_native_attached_exit_full_market_fill_verified": (
            native_attached_exit_observed
        ),
        "fifth_exit_has_no_positive_tp_price_observed": no_tp_price_observed,
        "fifth_attached_stop_exit_correlated_without_tp_price": (
            corroborated_sl_like_exit
        ),
        "fifth_historical_trigger_side": (
            "TP_CONFLICT" if fifth_ok else "UNVERIFIED"
        ),
        "fifth_explicit_stop_market_type": (
            fifth_result.get("historical_stop_loss_type")
            if fifth_ok else "UNVERIFIED"
        ),
        "native_sl_trigger_strictly_certified": False,
        "continuous_protection_during_amendment_certified": False,
        "entire_finished_stop_history_certified": False,
        "main_t100_strategy_parity_certified_by_these_pilots": False,
        "main_t100_restart_recovery_certified_by_these_pilots": False,
        "main_t100_trailing_stop_certified_by_these_pilots": False,
        "main_t100_cross_margin_transport_certified_by_these_pilots": False,
        "main_t100_production_adapter_certified_by_these_pilots": False,
        "live_authorization": False,
        "entry_allowed": False,
        "read_only": True,
        "mexc_orders_sent": 0,
    }


async def audit_read_only(client: Any, fingerprint: str) -> dict[str, Any]:
    fourth_result = await fourth.audit_fourth_pilot(client, fingerprint)
    fifth_result = await fifth.audit_fifth(client, fingerprint)
    return evaluate_exchange_observations(fourth_result, fifth_result)


async def _main() -> None:
    _allow_real_mode_env()
    if sys.platform != "darwin":
        raise pilot.TriggerPilotHalted("PILOT_EVIDENCE_MATRIX_MAC_ONLY")
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        result = await audit_read_only(client, fingerprint)
        print(json.dumps(result, indent=2, sort_keys=True))
        print("SIGNED_GET_ONLY_NO_MEXC_ORDERS_OR_JOURNAL_MUTATIONS=true")
    finally:
        await client.close()


def main() -> None:
    try:
        with OneShotProcessLock():
            asyncio.run(_main())
    except BaseException as exc:
        code = (
            str(exc) if isinstance(exc, pilot.TriggerPilotHalted)
            else type(exc).__name__
        )
        print("PILOT_EVIDENCE_MATRIX_HALTED=" + code)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
