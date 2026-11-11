"""Networkless holding process safety contract for legacy Render worker repurpose."""
from __future__ import annotations

import pytest
from app.t100_v40_render_executor_disabled_worker import require_disabled_worker

BASE={
    "T100_V40_EXECUTOR_DISABLED_WORKER":"true",
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
    "EXECUTION_ENABLED":"false",
    "TRADING_MODE":"paper",
}

def test_paper_no_database_no_legacy_exchange_passes():
    assert require_disabled_worker(BASE)

@pytest.mark.parametrize("k,v",[
    ("T100_V40_EXECUTOR_DISABLED_WORKER","false"),
    ("T100_EXECUTION_MODE","live"),
    ("T100_LIVE_ORDER_API_ENABLED","true"),
    ("T100_LIVE_KILL_SWITCH","false"),
    ("EXECUTION_ENABLED","true"),
    ("TRADING_MODE","live"),
    ("DATABASE_URL","postgresql://owner:pass@hostname/db"),
    ("MEXC_API_KEY","old_account_key"),
    ("MEXC_API_SECRET","old_account_secret"),
])
def test_any_nonisolated_or_trading_mode_fails_closed(k,v):
    assert not require_disabled_worker({**BASE,k:v})

def test_missing_enables_do_not_default_open():
    assert not require_disabled_worker({})
    for k in BASE:
        assert not require_disabled_worker({x:y for x,y in BASE.items() if x!=k})
