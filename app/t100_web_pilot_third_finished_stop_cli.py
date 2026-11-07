"""Real venue forensic check: third T100 pilot's finished native TP/SL row.

The subaccount is currently flat; this is signed GET ONLY. A completed
native SL can be canceled/invalidated after manual close, or may have been
executed separately. We distinguish actual venue state and trigger-side fields,
without making guesses from the MEXC UI or an absent `stopLossType`.

No order API, DB migration, trading authorization or journal rewrite.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from typing import Any

from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app import t100_web_pilot_native_stop_amendment_cli as native
from app import t100_web_pilot_third_close_audit_cli as close_audit
from app.t100_live_current_inventory_cli import (
    read_pilot_keychain_credentials,
)
from app.t100_web_pilot_one_shot_cli import (
    OneShotProcessLock, _allow_real_mode_env,
)

_FINISHED_STATE = {
    1: "UNTRIGGERED",
    2: "CANCELED",
    3: "EXECUTED",
    4: "INVALIDATED",
    5: "EXECUTION_FAILED",
}
_TRIGGER_SIDE = {0: "NOT_TRIGGERED", 1: "TAKE_PROFIT", 2: "STOP_LOSS"}
_MARKET_TYPE = {0: "MARKET", 1: "LIMIT"}


async def inspect_real_third_finished_stop(
    client: Any, fingerprint: str,
) -> dict[str, Any]:
    """Independently GET finished attached SL and real close deal details."""
    # Full native close-and-flat signed audit is a prerequisite. Reuse its
    # private sink only inside process to compare IDs; never print it.
    private: dict[str, Any] = {}
    close = await close_audit.audit_third_pilot(
        client, fingerprint, evidence_sink=private,
    )
    out: dict[str, Any] = {
        "status": "THIRD_FINISHED_TPSL_ORIGIN_UNVERIFIED",
        "account_bookended_flat_verified": (
            close.get("account_bookended_flat_verified") is True
        ),
        "third_market_close_fills_verified": (
            close.get("status") ==
            "THIRD_ONE_CONTRACT_REAL_CLOSE_AND_FLAT_VERIFIED_NONAUTHORIZING"
        ),
        "historical_order_archive_complete": (
            close.get("all_historical_orders_complete") is True
        ),
        "native_stop_market_type_certified": False,
        "native_sl_trigger_origin_certified": False,
        "native_amendment_live_certified": False,
        "nonflat_restart_recovery_certified": False,
        "live_authorization": False,
        "mexc_orders_sent": 0,
        "read_only": True,
    }
    if not out["third_market_close_fills_verified"] or not private:
        out["status"] = "THIRD_CLOSE_NOT_VERIFIED_STOP_AUDIT_HALTED"
        return out
    if (
        private.get("account_fingerprint_sha256") != fingerprint
        or private.get("journal_amend_post_attempted") is not False
        or private.get("entry_contracts") != "1"
    ):
        out["status"] = "PILOT_IDENTITY_OR_AMENDMENT_JOURNAL_CONFLICT"
        return out

    original_path = pilot.JOURNAL
    try:
        pilot.JOURNAL = native.THIRD_JOURNAL
        journal = native._check_journal(fingerprint)
    finally:
        pilot.JOURNAL = original_path
    safe = pilot._GetOnly(client)
    rows, pages = await pilot._history(
        safe, private["entry_order_id"], private["position_id"],
    )
    out["finished_stop_pages_read"] = pages
    out["matching_finished_stop_row_count"] = len(rows)
    out["finished_stop_history_complete"] = False
    if len(rows) != 1:
        out["status"] = (
            "MULTIPLE_FINISHED_STOP_ROWS_AMBIGUOUS"
            if len(rows) > 1 else "FINISHED_STOP_NOT_FOUND_BOUNDED_HISTORY"
        )
        return out
    row = rows[0]
    if (
        pilot.id_string(row.get("id")) is None
        or pilot.id_string(row.get("orderId")) != private["entry_order_id"]
        or pilot.id_string(row.get("positionId")) != private["position_id"]
        or row.get("symbol") != pilot.SYMBOL
        or type(row.get("positionType")) is not int
        or row["positionType"] != 2
    ):
        out["status"] = "FINISHED_STOP_IDENTITY_OR_SIDE_INVALID"
        return out
    state = row.get("state")
    trigger = row.get("triggerSide")
    is_finished = row.get("isFinished")
    sl_type = row.get("stopLossType")
    out.update({
        "finished_stop_state": (
            _FINISHED_STATE.get(state, "UNRECOGNIZED")
            if type(state) is int else "NOT_REPORTED"
        ),
        "finished_trigger_side": (
            _TRIGGER_SIDE.get(trigger, "UNRECOGNIZED")
            if type(trigger) is int else "NOT_REPORTED"
        ),
        "finished_terminal_flag": (
            "TERMINAL" if type(is_finished) is int and is_finished == 1
            else "NOT_TERMINAL_OR_NOT_REPORTED"
        ),
        "finished_stop_loss_type": (
            _MARKET_TYPE.get(sl_type, "UNRECOGNIZED")
            if type(sl_type) is int else "FIELD_NOT_REPORTED"
        ),
        "finished_stop_error_code": (
            "ZERO" if type(row.get("errorCode")) is int
            and row["errorCode"] == 0
            else "NONZERO" if type(row.get("errorCode")) is int
            else "NOT_REPORTED"
        ),
        "finished_stop_price_matches_original_entry": (
            pilot.positive(row.get("stopLossPrice")) ==
            pilot.positive(journal["stop_price"])
        ),
        "finished_stop_full_one_contract_volume": (
            row.get("profitLossVolType") == "SEPARATE"
            and pilot.positive(row.get("stopLossVol")) == pilot.D(1)
        ),
        "finished_stop_no_reverse": (
            type(row.get("stopLossReverse")) is int
            and row["stopLossReverse"] == 2
        ),
        "generated_close_order_links_third_trade": (
            pilot.id_string(row.get("placeOrderId")) ==
            private["close_order_id"]
        ),
        "third_close_origin": close.get("close_origin", "UNVERIFIED"),
    })
    out["native_stop_market_type_certified"] = (
        out["finished_stop_loss_type"] == "MARKET"
        and out["finished_terminal_flag"] == "TERMINAL"
        and out["finished_stop_price_matches_original_entry"]
        and out["finished_stop_full_one_contract_volume"]
        and out["finished_stop_no_reverse"]
        and out["finished_stop_error_code"] == "ZERO"
    )
    # Very strict: even an exact market close generated from the TP/SL
    # row is not an *SL* trigger if the venue says 1=TP or omits its type.
    out["native_sl_trigger_origin_certified"] = (
        out["native_stop_market_type_certified"]
        and out["finished_stop_state"] == "EXECUTED"
        and out["finished_trigger_side"] == "STOP_LOSS"
        and out["generated_close_order_links_third_trade"]
    )
    out["status"] = "THIRD_FINISHED_STOP_FORENSICS_OBSERVED_NONAUTHORIZING"
    return out


async def _main() -> None:
    _allow_real_mode_env()
    if sys.platform != "darwin":
        raise pilot.TriggerPilotHalted("THIRD_STOP_HISTORY_MAC_ONLY")
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fp = hashlib.sha256(key.encode("utf-8")).hexdigest()
    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        result = await inspect_real_third_finished_stop(client, fp)
        print(json.dumps(result, indent=2, sort_keys=True))
        print("MEXC_SIGNED_GET_ONLY_NO_ORDERS=true")
    finally:
        await client.close()


def main() -> None:
    try:
        with OneShotProcessLock():
            asyncio.run(_main())
    except BaseException as exc:
        reason = (
            str(exc) if isinstance(exc, pilot.TriggerPilotHalted)
            else type(exc).__name__
        )
        print("THIRD_STOP_HISTORY_HALTED=" + reason)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
