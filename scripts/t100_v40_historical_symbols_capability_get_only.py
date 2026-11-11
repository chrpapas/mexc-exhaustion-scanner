"""Current MEXC GET-only capability sweep of eight VERIFIED historical Stage2 symbols.

These EXACT eight symbols were returned from the operator's independent
2026-11-11 08:10 UTC real production scanner / 49-prior-Min30 causal audit.
They are a FIXED historical RESEARCH sample, not fresh Stage2 signals and
not V40 entry candidates. This is not a replay/forecast, execution test,
liquidity certificate, profitability estimate, or risk permission.

Reuse the already independently exercised single-symbol pilot capability
observer. For EACH symbol it separately requires two signed full-account
flat/hedge bookends, repeated private symbol-scoped risk_limit plus
position/leverage GETs, and a public country-contract GET; it DOES NOT
read market depth, check stressed margin, or test trade-size slippage.
No DB, scanner, GitHub, Render, BOXWAVE or MEXC POST/stop/order calls.

Local manual-only Mac command:
 T100_EXECUTION_MODE=paper T100_LIVE_ORDER_API_ENABLED=false \
 T100_LIVE_KILL_SWITCH=true \
 .venv/bin/python -m scripts.t100_v40_historical_symbols_capability_get_only \
    --keychain-service t100225-mexc-web-pilot-subaccount-v1

Keychain is the dedicated WEB pilot service, not BOXWAVE, monitoring or
the older pilot. No credentials are printed or written. A successful
capability response DOES NOT grant trading rights and the account must
stay flat for each per-symbol witness.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import asdict,dataclass
from pathlib import Path
from typing import Any,Mapping

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted,require_readonly_controls,
)
from scripts.t100_v40_pilot_get_only import (
    KEY_ACCOUNT,SECRET_ACCOUNT,_from_keychain,
)
from scripts.t100_v40_pilot_capability_get_only import (
    PilotCapabilityReport,observe_pilot_capability,
)

WEB_PILOT_KEYCHAIN_SERVICE="t100225-mexc-web-pilot-subaccount-v1"
# Grounded by user's 8/8 actual Stage2 historical audit, not invented
# watchlist or fresh executable Stage2 signal selection.
HISTORICAL_SAMPLE=(
    ("IDOL_USDT","LOW_100",496),
    ("ARIA_USDT","HIGH_225",454),
    ("CLANKER_USDT","LOW_100",433),
    ("LYN_USDT","LOW_100",398),
    ("BEAMX_USDT","LOW_100",386),
    ("AIN_USDT","LOW_100",375),
    ("APR_USDT","HIGH_225",361),
    ("DOOD_USDT","HIGH_225",311),
)
_VALID_SETTINGS_STATUSES=frozenset({
    "PILOT_PUBLIC_CONTRACT_AND_PRIVATE_SHORT_CROSS_TIER_OBSERVED_NONAUTHORIZING",
})
MAX_SYMBOLS=len(HISTORICAL_SAMPLE)


@dataclass(frozen=True)
class HistoricalSymbolSettings:
    symbol:str
    historical_tier:str
    historical_signal_id:int
    status:str
    public_contract_eligible_observed:bool=False
    one_contract_on_public_lot_grid:bool=False
    private_short_risk_limit_observed:bool=False
    short_margin_mode_observed:str="UNKNOWN"
    private_capability_drift_source:str="NONE"
    private_risk_drift_field_labels:tuple[str,...]=()
    private_risk_row_order_normalized:bool=False
    short_leverage_observed:int|None=None
    current_short_cross_1x_observed:bool=False
    historical_only:bool=True
    current_market_depth_or_liquidity_verified:bool=False
    actual_entry_size_verified:bool=False
    entry_allowed:bool=False
    live_authorization:bool=False


@dataclass(frozen=True)
class HistoricalCapabilitySweep:
    status:str
    planned_symbols:int=MAX_SYMBOLS
    checked_symbols:int=0
    cross_1x_contract_settings_observed:int=0
    symbols:tuple[HistoricalSymbolSettings,...]=()
    limited_research_sample:bool=True
    historical_signal_causality_revalidated_in_this_run:bool=False
    no_fresh_signal_checked:bool=True
    current_market_depth_verified:bool=False
    full_cross_collateral_verified:bool=False
    actual_order_execution_certified:bool=False
    independent_nonflat_restart_certified:bool=False
    api_key_futures_write_permission_verified:bool=False
    read_only:bool=True
    database_writes:int=0
    mexc_posts:int=0
    entry_allowed:bool=False
    live_authorization:bool=False


def _halt(status:str)->HistoricalCapabilitySweep:
    return HistoricalCapabilitySweep(status=status)


def _safe_row(
    symbol:str,tier:str,signal_id:int,
    observation:PilotCapabilityReport,
)->HistoricalSymbolSettings:
    # Do not trust even an injected "passing" observer that reports POST,
    # DB mutations or live permissions. A sample row is research-only.
    if (
        type(observation) is not PilotCapabilityReport
        or observation.symbol!=symbol
        or observation.mexc_posts!=0 or observation.db_mutations!=0
        or observation.entry_allowed is not False
        or observation.live_authorization is not False
        or observation.api_key_futures_write_permission_verified is not False
        or type(observation.status) is not str
        or re.fullmatch(r"[A-Z0-9_]+",observation.status) is None
        or observation.short_margin_mode_observed not in (
            "UNKNOWN","CROSS","ISOLATED"
        )
        or observation.private_capability_drift_source not in (
            "NONE","RISK_LIMIT","POSITION_LEVERAGE","BOTH"
        )
        or type(observation.private_risk_row_order_normalized) is not bool
        or type(observation.private_risk_drift_field_labels) is not tuple
        or any(
            type(label) is not str
            or re.fullmatch(
                r"(LONG|SHORT)_(SYMBOL|POSITION_TYPE|LEVEL|MAXVOL|MAX_LEVERAGE|MMR|IMR|OPEN_TYPE|LEVERAGE|LIMIT_BY_SYS|CURRENT_MMR|OTHER_FIELD)"
                r"|RESPONSE_SHAPE_CHANGED|ROW_SET_OR_SHAPE_CHANGED|ROW_ORDER_ONLY|UNCLASSIFIED_CHANGE",
                label,
            ) is None
            for label in observation.private_risk_drift_field_labels
        )
        or len(observation.private_risk_drift_field_labels)>24
        or (
            observation.short_leverage_observed is not None
            and (
                type(observation.short_leverage_observed) is not int
                or not 1<=observation.short_leverage_observed<=1000
            )
        )
    ):
        return HistoricalSymbolSettings(
            symbol=symbol,historical_tier=tier,
            historical_signal_id=signal_id,
            status="HISTORICAL_SYMBOL_EVIDENCE_INVARIANT_FAILED_HALT",
        )
    status=observation.status
    proven=status in _VALID_SETTINGS_STATUSES and all((
        observation.flat_hedge_account_bookended is True,
        observation.public_futures_contract_eligible_observed is True,
        observation.private_short_position_leverage_observed is True,
        observation.current_private_short_risk_limit_observed is True,
        observation.current_private_short_cross_1x_tier_observed is True,
        observation.current_short_tier_supports_one_contract is True,
        observation.one_contract_on_public_lot_grid is True,
        observation.current_contract_tick_observed is True,
        observation.private_capability_drift_source=="NONE",
        observation.private_risk_drift_field_labels==(),
        observation.short_margin_mode_observed=="CROSS",
        observation.short_leverage_observed==1,
    ))
    if status in _VALID_SETTINGS_STATUSES and not proven:
        status="HISTORICAL_SYMBOL_PARTIAL_PASSED_OBSERVATION_REJECTED_HALT"
    return HistoricalSymbolSettings(
        symbol=symbol,historical_tier=tier,historical_signal_id=signal_id,
        status=status,
        public_contract_eligible_observed=(
            observation.public_futures_contract_eligible_observed is True
        ),
        one_contract_on_public_lot_grid=(
            observation.one_contract_on_public_lot_grid is True
        ),
        private_short_risk_limit_observed=(
            observation.current_private_short_risk_limit_observed is True
        ),
        short_margin_mode_observed=observation.short_margin_mode_observed,
        short_leverage_observed=observation.short_leverage_observed,
        private_capability_drift_source=observation.private_capability_drift_source,
        private_risk_drift_field_labels=observation.private_risk_drift_field_labels,
        private_risk_row_order_normalized=observation.private_risk_row_order_normalized,
        current_short_cross_1x_observed=proven,
    )


async def sweep_historical_settings(
    *,key:str,secret:str,env:Mapping[str,str],max_symbols:int=MAX_SYMBOLS,
)->HistoricalCapabilitySweep:
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return _halt("HISTORICAL_SETTINGS_PAPER_KILL_REQUIRED_HALT")
    if (
        type(key) is not str or not key
        or type(secret) is not str or not secret
        or type(max_symbols) is not int
        or not 1<=max_symbols<=MAX_SYMBOLS
    ):
        return _halt("HISTORICAL_SETTINGS_INPUT_INVALID_HALT")
    results=[]
    for symbol,tier,sid in HISTORICAL_SAMPLE[:max_symbols]:
        try:
            outcome=await asyncio.wait_for(
                observe_pilot_capability(
                    key=key,secret=secret,symbol=symbol,
                ),timeout=65,
            )
        except Exception:
            results.append(HistoricalSymbolSettings(
                symbol=symbol,historical_tier=tier,historical_signal_id=sid,
                status="HISTORICAL_SYMBOL_GET_UNVERIFIED_HALT",
            ))
            continue
        row=_safe_row(symbol,tier,sid,outcome)
        results.append(row)
        # If the formerly flat pilot is now occupied or unverified, stop
        # querying the remaining symbols; do not pretend it is stable.
        if outcome.status=="PILOT_FLAT_GET_BOOKENDS_REQUIRED_HALT":
            break
    count=sum(x.current_short_cross_1x_observed for x in results)
    return HistoricalCapabilitySweep(
        status=(
            "HISTORICAL_SYMBOLS_FULL_READONLY_GET_SWEEP_NONAUTHORIZING"
            if len(results)==max_symbols else
            "HISTORICAL_SYMBOLS_ACCOUNT_PRECHECK_ABORT_NONAUTHORIZING"
        ),
        planned_symbols=max_symbols,
        checked_symbols=len(results),
        cross_1x_contract_settings_observed=count,
        symbols=tuple(results),
    )


async def _main(args:argparse.Namespace,env:Mapping[str,str])->HistoricalCapabilitySweep:
    if sys.platform!="darwin":
        return _halt("HISTORICAL_SETTINGS_MAC_ONLY_HALT")
    if args.keychain_service!=WEB_PILOT_KEYCHAIN_SERVICE:
        return _halt("HISTORICAL_SETTINGS_PILOT_SCOPE_INVALID_HALT")
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return _halt("HISTORICAL_SETTINGS_PAPER_KILL_REQUIRED_HALT")
    if type(args.max_symbols) is not int or not 1<=args.max_symbols<=MAX_SYMBOLS:
        return _halt("HISTORICAL_SETTINGS_BATCH_SIZE_INVALID_HALT")
    try:
        key=_from_keychain(args.keychain_service,KEY_ACCOUNT)
        secret=_from_keychain(args.keychain_service,SECRET_ACCOUNT)
    except Exception:
        return _halt("HISTORICAL_SETTINGS_KEYCHAIN_UNAVAILABLE_HALT")
    return await sweep_historical_settings(
        key=key,secret=secret,env=env,max_symbols=args.max_symbols,
    )


def main()->None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--keychain-service",required=True)
    p.add_argument("--max-symbols",type=int,default=MAX_SYMBOLS)
    p.add_argument(
        "--output",default="~/Downloads/t100_v40_historical_symbols_settings_get_only.json"
    )
    args=p.parse_args()
    try:
        result=asyncio.run(_main(args,dict(os.environ)))
    except Exception:
        result=_halt("HISTORICAL_SETTINGS_DIAGNOSTIC_FAILED_HALT")
    path=Path(args.output).expanduser()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SystemExit("HISTORICAL_SETTINGS_OUTPUT_PATH_UNSAFE")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(asdict(result),indent=2,sort_keys=True)+"\n")
    path.chmod(0o600)
    print("HISTORICAL_ONLY=true MEXC_GET_ONLY=true POST_COUNT=0 DB_WRITES=0")
    print("REPORT_SAVED="+str(path))
    print(json.dumps(asdict(result),sort_keys=True))


if __name__=="__main__":
    main()
