"""V40 real first-position risk evidence is only promoted after all sources."""
import asyncio
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from decimal import Decimal as D
from types import SimpleNamespace
import pytest

from app.t100_live_v40_entry_coordinator import V40ContractTerms
from app.t100_live_v40_stage2_source import V40SourceCandidate
from app.t100_live_v40_candidate_market_witness import (
    V40CandidateQuote,MexcV40CandidateMarketWitness,
)
from app.t100_v40_canary_daily_loss import V40DailyLossEvidence
from app.t100_v40_canary_key_permissions import V40KeyPermissionObservation
from app.t100_v40_render_journal_readonly import V40RuntimeJournalObservation
from app.t100_v40_first_canary_risk_witness import (
    V40CanaryRiskHalted,V40FirstCanaryRiskWitness,
    assess_v40_first_canary_risk,
)

NOW=datetime(2026,11,13,12,0,0,tzinfo=timezone.utc)


def signal():
    return V40SourceCandidate(
        signal_id=1001,strategy_id="T100_200",symbol="EXAMPLE_USDT",
        admission_open_ts=int(NOW.timestamp())-90,
        feature_open_ts=int(NOW.timestamp())-1890,
        entry_reference_price=100.,
        stage1_atr7_pct=5.8,stage1_return_24h=0.22,
        physical_episode_seq=4,tier="HIGH_225",prior_bars=tuple(),
        pre24_vol_pct=18.0,observed_age_seconds=90.,
    )


def quote(ms=None):
    return V40CandidateQuote(
        status="V40_GET_ONLY_FLAT_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING",
        symbol="EXAMPLE_USDT",observed_at_ms=(
            int(NOW.timestamp()*1000) if ms is None else ms
        ),
        book_age_ms=100,
        account_equity_usdt=D("56.37671495"),
        account_available_usdt=D("56.37671495"),
        candidate_contract=V40ContractTerms(
            contract_size=D(".001"),volume_step=D("1"),
            min_volume=D("1"),max_volume=D("1000"),
            price_tick=D("0.01"),private_order_permission_verified=False,
            market_state_and_country_verified=True,
        ),
        current_private_short_max_vol=D("1000"),
        top_bid=D("100"),top_ask=D("101"),
        bookended_flat_account=True,hedge_mode_observed=True,
        private_risk_row_observed=True,
        private_short_cross_1x_settings_bookended=True,
    )


def assets():
    return [{
        "currency":"USDT","equity":"56.37671495",
        "cashBalance":"56.37671495",
        "availableBalance":"56.37671495",
        "positionMargin":"0","unrealized":"0",
        "debtAmount":"0","bonus":"0","frozenBalance":"0",
        "contributeMarginAmount":"0",
    }]


def key():
    return V40KeyPermissionObservation(
        status="V40_KEY_INFO_FUTURES_WRITE_SCOPE_OBSERVED_NONAUTHORIZING",
        api_key_identity_verified=True,api_key_active_verified=True,
        futures_account_read_scope_verified=True,
        futures_order_read_scope_verified=True,
        futures_order_write_scope_verified=True,
    )


def pnl():
    return V40DailyLossEvidence(
        status="V40_DAILY_LOSS_TWO_COMPLETE_SIGNED_GET_PASSES_NONAUTHORIZING",
        realized_today_loss_usdt="0.1",closed_positions_today=1,
        total_historical_positions=8,complete_two_passes=True,
        complete_history_verified=True,safely_below_12_50=True,
    )


def journal():
    return V40RuntimeJournalObservation(
        status="V40_FIRST_POSITION_JOURNAL_EMPTY_AND_READONLY_VERIFIED",
        runtime_role_verified=True,repeatable_read_readonly_verified=True,
        v40_tables_observed=True,v40_dispatch_state_unresolved_absent=True,
        v40_no_prior_claims_or_slots=True,legacy_unresolved_absent=True,
        first_position_journal_clear=True,
    )


def evaluate(**overrides):
    args=dict(signal=signal(),quote_first=quote(),
              quote_second=quote(int(NOW.timestamp()*1000)+100),
              assets=assets(),key=key(),pnl=pnl(),
              journal=journal(),observed_at=NOW+timedelta(seconds=1))
    args.update(overrides)
    return assess_v40_first_canary_risk(**args)


def test_real_dedicated_flat_witness_produces_current_risk_and_terms_not_permit():
    r=evaluate()
    assert r.status=="V40_FIRST_FLAT_CANARY_CURRENT_RISK_OBSERVED_NONAUTHORIZING"
    assert r.current_risk is not None and r.contract_terms is not None
    assert r.current_risk.current_owned_positions==0
    assert r.current_risk.realized_today_loss_usdt==D("0.1")
    assert r.current_risk.available_margin_usdt==D("56.37671495")
    assert r.current_risk.cross_collateral_verified
    assert r.current_risk.no_unresolved_mutations
    assert r.contract_terms.private_order_permission_verified
    assert not r.future_cross_tier_and_native_stop_execution_certified
    assert not r.live_authorization and not r.entry_allowed
    assert r.mexc_posts==r.database_writes==0


@pytest.mark.parametrize("updated,expected",[
    ({"assets":assets()+[{"currency":"BTC","equity":"0.01"}]},
     "V40_CANARY_RISK_UNENCUMBERED_CROSS_WALLET_UNVERIFIED"),
    ({"assets":[{**assets()[0],"debtAmount":"1"}]},
     "V40_CANARY_RISK_UNENCUMBERED_CROSS_WALLET_UNVERIFIED"),
    ({"assets":[{**assets()[0],"bonus":"0.01"}]},
     "V40_CANARY_RISK_UNENCUMBERED_CROSS_WALLET_UNVERIFIED"),
    ({"assets":[{**assets()[0],"cashBalance":"50"}]},
     "V40_CANARY_RISK_UNENCUMBERED_CROSS_WALLET_UNVERIFIED"),
    ({"assets":[{**assets()[0],"availableBalance":"1"}]},
     "V40_CANARY_RISK_UNENCUMBERED_CROSS_WALLET_UNVERIFIED"),
    ({"quote_second":replace(quote(),account_available_usdt=D("50"))},
     "V40_CANARY_RISK_MARKET_OR_WALLET_BOOKEND_DRIFT"),
    ({"quote_second":replace(quote(),observed_at_ms=int(NOW.timestamp()*1000)-31000)},
     "V40_CANARY_RISK_MARKET_WITNESS_STALE"),
    ({"quote_first":replace(quote(),private_short_cross_1x_settings_bookended=False)},
     "V40_CANARY_RISK_MARKET_FLAT_CROSS_UNVERIFIED"),
    ({"key":replace(key(),futures_order_write_scope_verified=False)},
     "V40_CANARY_RISK_CURRENT_PRIVATE_KEY_SCOPE_MISSING"),
    ({"pnl":replace(pnl(),complete_history_verified=False)},
     "V40_CANARY_RISK_SIGNED_DAILY_LOSS_UNVERIFIED"),
    ({"pnl":replace(pnl(),realized_today_loss_usdt="12.50")},
     "V40_CANARY_RISK_DAILY_LOSS_CAP_REACHED"),
    ({"journal":replace(journal(),first_position_journal_clear=False)},
     "V40_CANARY_RISK_DURABLE_MUTATIONS_NOT_CLEAR"),
])
def test_every_missing_independent_gate_refuses_risk_certification(updated,expected):
    r=evaluate(**updated)
    assert r.status==expected
    assert r.current_risk is None and r.contract_terms is None
    assert not r.entry_allowed and not r.live_authorization


def test_production_witness_reobserves_account_after_history_and_sql():
    events=[]
    market=object.__new__(MexcV40CandidateMarketWitness)
    async def observe(signal):
        events.append("MEXC_FULL_BOOKEND")
        return quote(int(datetime.now(timezone.utc).timestamp()*1000))
    async def account_assets():
        events.append("MEXC_SIGNED_ASSETS")
        return assets()
    market.observe=observe
    market.private=SimpleNamespace(raw_account_assets=account_assets)
    async def permissions():
        events.append("KEY_WRITE_IDENTITY")
        return key()
    async def history():
        events.append("TODAY_PNL")
        return pnl()
    async def runtime_journal():
        events.append("RUNTIME_SQL")
        return journal()
    witness=V40FirstCanaryRiskWitness(
        market=market,get_permissions=permissions,
        get_daily_loss=history,get_runtime_journal=runtime_journal,
    )
    # Inject a fresh clock into the function-under-test via a timestamp that
    # is always within 900 sec of datetime.now rather than the static fixture.
    live=replace(signal(),admission_open_ts=int(datetime.now(timezone.utc).timestamp())-90)
    result=asyncio.run(witness.inspect(live))
    assert result[0].no_unresolved_mutations
    assert events==[
        "MEXC_FULL_BOOKEND","KEY_WRITE_IDENTITY","TODAY_PNL",
        "RUNTIME_SQL","MEXC_SIGNED_ASSETS","MEXC_FULL_BOOKEND",
    ]


def test_market_or_sql_failure_never_exposes_credentials():
    market=object.__new__(MexcV40CandidateMarketWitness)
    async def fail(s):
        raise RuntimeError("PRIVATE_MEXC_API_SECRET")
    market.observe=fail
    witness=V40FirstCanaryRiskWitness(
        market=market,get_permissions=lambda:None,
        get_daily_loss=lambda:None,get_runtime_journal=lambda:None,
    )
    with pytest.raises(V40CanaryRiskHalted) as error:
        asyncio.run(witness.inspect(signal()))
    assert "PRIVATE_MEXC" not in str(error.value)
