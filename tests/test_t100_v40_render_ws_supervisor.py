"""Offline bounded WS companion: primary V40 signed GET remains independent."""
import asyncio
import json

from app.t100_v40_render_private_ws_get_only import (
    FLAG,V40WsReadOnlyPreflight,
)
from app.t100_v40_render_ws_supervisor import run_with_optional_ws_preflight


def test_flag_off_leaves_pure_monitor_baseline_unchanged():
    events=[]
    async def monitor(env):
        events.append("monitor")
        return 0
    async def ws(env):
        events.append("ws")
        raise AssertionError("no ws when flag off")
    out=asyncio.run(run_with_optional_ws_preflight(
        {},monitor=monitor,ws_observe=ws,
    ))
    assert out==0 and events==["monitor"]


def test_ws_timeout_and_bad_result_not_construed_as_trading_readiness():
    events=[]
    logged=[]
    async def ws(env):
        events.append("ws")
        return {"login_verified":True,"entry_allowed":True}
    async def monitor(env):
        events.append("monitor")
        # One loop yield allows isolated task to finish before we exit.
        await asyncio.sleep(.01)
        return 0
    out=asyncio.run(run_with_optional_ws_preflight(
        {FLAG:"true"},monitor=monitor,ws_observe=ws,
        emit=logged.append,
    ))
    assert out==0
    assert events[0]=="monitor" or events[0]=="ws"
    assert len(logged)==1
    assert json.loads(logged[0])["status"]=="V40_WS_PREFLIGHT_UNVERIFIED_OR_TIMED_OUT_HALT"
    assert json.loads(logged[0])["exchange_posts"]==0


def test_realistic_successful_ws_login_does_not_enable_orders():
    logged=[]
    async def ws(env):
        return V40WsReadOnlyPreflight(
            "V40_WS_LOGIN_FILTER_OBSERVED_LIFECYCLE_NOT_CERTIFIED",
            ws_login_observed=True,ws_filter_observed=True,
        )
    async def monitor(env):
        await asyncio.sleep(.01)
        return 0
    out=asyncio.run(run_with_optional_ws_preflight(
        {FLAG:"true"},monitor=monitor,ws_observe=ws,
        emit=logged.append,
    ))
    assert out==0
    v=json.loads(logged[0])
    assert v["ws_login_observed"] is True
    assert v["live_authorization"] is False
    assert v["entry_allowed"] is False
    assert v["independent_guardian_connected"] is False
    assert v["native_stop_execution_certified"] is False
    assert v["exchange_posts"]==v["database_writes"]==0


def test_primary_monitor_failure_stays_terminal_and_ws_is_cancelled():
    started=asyncio.Event()
    finished=asyncio.Event()
    async def ws(env):
        started.set()
        try:
            await asyncio.sleep(100)
        finally:
            finished.set()
        return V40WsReadOnlyPreflight("NEVER")
    async def monitor(env):
        await started.wait()
        return 2
    out=asyncio.run(run_with_optional_ws_preflight(
        {FLAG:"true"},monitor=monitor,ws_observe=ws,
    ))
    assert out==2 and started.is_set() and finished.is_set()


def test_failed_websocket_does_not_crash_primary_readonly_monitor():
    logged=[]
    async def ws(env):
        raise RuntimeError("EXPOSED_MEXC_API_SECRET")
    async def monitor(env):
        await asyncio.sleep(.01)
        return 0
    out=asyncio.run(run_with_optional_ws_preflight(
        {FLAG:"true"},monitor=monitor,ws_observe=ws,
        emit=logged.append,
    ))
    assert out==0 and len(logged)==1
    assert "EXPOSED" not in logged[0]
    assert json.loads(logged[0])["entry_allowed"] is False


def test_optional_futures_key_permission_status_never_authorizes_a_trade():
    from app.t100_v40_canary_key_permissions import (
        _FLAG as KEY_FLAG,V40KeyPermissionObservation,
    )
    logged=[]
    async def monitor(env):
        await asyncio.sleep(.02)
        return 0
    async def keys(env):
        return V40KeyPermissionObservation(
            "V40_KEY_INFO_FUTURES_WRITE_SCOPE_OBSERVED_NONAUTHORIZING",
            api_key_identity_verified=True,api_key_active_verified=True,
            futures_account_read_scope_verified=True,
            futures_order_read_scope_verified=True,
            futures_order_write_scope_verified=True,
        )
    out=asyncio.run(run_with_optional_ws_preflight(
        {KEY_FLAG:"true"},monitor=monitor,key_observe=keys,
        emit=logged.append,
    ))
    assert out==0 and len(logged)==1
    data=json.loads(logged[0])
    assert data["futures_order_write_scope_verified"] is True
    assert data["live_authorization"] is False
    assert data["entry_allowed"] is False
    assert data["mexc_posts"]==data["database_writes"]==0


def test_key_get_error_does_not_interrupt_primary_signal_monitor():
    from app.t100_v40_canary_key_permissions import _FLAG as KEY_FLAG
    logged=[]
    async def monitor(env):
        await asyncio.sleep(.02)
        return 0
    async def keys(env):
        raise RuntimeError("SENSITIVE_MEXC_API_SECRET")
    out=asyncio.run(run_with_optional_ws_preflight(
        {KEY_FLAG:"true"},monitor=monitor,key_observe=keys,
        emit=logged.append,
    ))
    assert out==0 and len(logged)==1
    assert "SENSITIVE" not in logged[0]
    assert json.loads(logged[0])["status"]=="V40_KEY_INFO_UNEXPECTED_HALT"


def test_key_probe_is_not_started_when_not_explicitly_opted_in():
    from app.t100_v40_canary_key_permissions import _FLAG as KEY_FLAG
    events=[]
    async def monitor(_):
        events.append("monitor")
        return 0
    async def keys(_):
        events.append("key")
        raise AssertionError("key probe must not run")
    assert asyncio.run(run_with_optional_ws_preflight(
        {KEY_FLAG:"false"},monitor=monitor,key_observe=keys,
    ))==0
    assert events==["monitor"]


def test_daily_loss_companion_never_upgrades_primary_signal_or_order_permission():
    from app.t100_v40_canary_daily_loss import (
        FLAG as LOSS_FLAG,V40DailyLossEvidence,
    )
    observed=[]
    async def monitor(env):
        await asyncio.sleep(.02)
        return 0
    async def daily(env):
        return V40DailyLossEvidence(
            "V40_DAILY_LOSS_TWO_COMPLETE_SIGNED_GET_PASSES_NONAUTHORIZING",
            realized_today_loss_usdt="0",complete_two_passes=True,
            complete_history_verified=True,safely_below_12_50=True,
        )
    out=asyncio.run(run_with_optional_ws_preflight(
        {LOSS_FLAG:"true"},monitor=monitor,
        daily_observe=daily,emit=observed.append,
    ))
    assert out==0 and len(observed)==1
    r=json.loads(observed[0])
    assert r["complete_history_verified"] is True
    assert r["entry_allowed"] is False and r["live_authorization"] is False
    assert r["mexc_posts"]==r["database_writes"]==0


def test_daily_history_error_is_redacted_and_does_not_hide_monitor_failure():
    from app.t100_v40_canary_daily_loss import FLAG as LOSS_FLAG
    messages=[]
    async def monitor(env):
        await asyncio.sleep(.02)
        return 2
    async def daily(env):
        raise RuntimeError("MEXC_PRIVATE_API_SECRET_LEAK")
    out=asyncio.run(run_with_optional_ws_preflight(
        {LOSS_FLAG:"true"},monitor=monitor,
        daily_observe=daily,emit=messages.append,
    ))
    assert out==2 and len(messages)==1
    assert "MEXC_PRIVATE" not in messages[0]
    assert json.loads(messages[0])["status"]=="V40_DAILY_LOSS_UNEXPECTED_OR_TIMED_OUT_HALT"
