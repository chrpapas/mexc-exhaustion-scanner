"""Offline-only V40 Discord event routing tests. No HTTP, DB, or exchange POST."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import app.t100_v40_discord_events as notices
import app.t100_v40_render_stage2_scanner as scanner
import app.t100_v40_render_real_canary_worker as worker


T = datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)
HOOK = ("https://discord.com/api/webhooks/"
        "123456789012345678/ABCdef12345678901234567890abcDEF")


def test_discord_webhook_allowlist_and_priority_never_guess_secrets():
    assert notices.webhook_from_env({}) is None
    assert notices.webhook_from_env({"T100_V40_DISCORD_WEBHOOK_URL":
                                    "https://example.com/steal"}) is None
    assert notices.webhook_from_env({"DISCORD_WEBHOOK_URL":HOOK}) == HOOK
    assert notices.webhook_from_env({
        "T100_V40_DISCORD_WEBHOOK_URL":HOOK,
        "DISCORD_WEBHOOK_URL":"https://invalid.local/api/webhooks/123/abc",
    }) == HOOK


def test_stage2_is_yellow_and_explicitly_not_a_trade():
    receipt=scanner.V40ObservedSignal(
        signal_id=606,symbol="PIPPIN_USDT",tier="HIGH_225",
        p2_at=T,pre24_vol_pct=28.4513,
    )
    event=notices.stage2_notice(receipt)
    assert event is not None
    assert event.severity == "warning"
    assert "SIGNAL ONLY" in event.description
    assert "no exchange order" in event.description
    assert "PIPPIN_USDT" in event.title
    assert notices.stage2_notice(SimpleNamespace(
        signal_id=606,symbol="PIPPIN_USDT",tier="HIGH_225",
        p2_at=T,pre24_vol_pct=30.0,
    )) is None


def test_execution_error_is_red_and_never_claims_entry():
    event=notices.trader_notice(SimpleNamespace(
        status="V40_FIRST_CANARY_CURRENT_RISK_DEPTH_OR_ACCOUNT_HALT",
        signal_id=606,precheck_failure_code="V40_CANARY_RISK_MARKET_WITNESS_STALE",
        order_post_may_have_occurred=False,
    ))
    assert event is not None
    assert event.severity == "critical"
    assert "NO ORDER POST" in event.description
    assert "V40_CANARY_RISK_MARKET_WITNESS_STALE" in event.description
    assert notices.trader_notice(SimpleNamespace(
        status="V40_FIRST_CANARY_REJECTED_SIGNAL_NO_RETRY",
        signal_id=606,
    )) is None


def test_green_only_after_verified_exchange_entry():
    name="V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS"
    assert notices.trader_notice(SimpleNamespace(
        status=name,verified_entry=False,signal_id=606,
    )) is None
    event=notices.trader_notice(SimpleNamespace(
        status=name,verified_entry=True,signal_id=606,
    ))
    assert event is not None
    assert event.severity == "success"
    assert "confirmed entry" in event.description


def test_unknown_order_reports_possible_post_not_false_no_order():
    event=notices.trader_notice(SimpleNamespace(
        status="V40_FIRST_CANARY_UNKNOWN_AFTER_ENTRY_BOUNDARY_RECONCILE_ONLY",
        signal_id=606,order_post_may_have_occurred=True,
    ))
    assert event is not None
    assert event.severity == "critical"
    assert "may" in event.description
    assert "NO ORDER" not in event.description


def test_close_notice_requires_double_verified_emergency_closure():
    complete=SimpleNamespace(
        durable_checkpoint_and_slot_closed=True,
        signed_close_and_flat_verified=True,
        status="V40_EMERGENCY_CLOSE_CHECKPOINT_INTENT_AND_SLOT_CLOSED_VERIFIED",
    )
    notice=notices.verified_emergency_close_notice(complete)
    assert notice is not None
    assert "Not a reported SL75 hit" in notice.description
    assert notices.verified_emergency_close_notice(SimpleNamespace(
        durable_checkpoint_and_slot_closed=True,
        signed_close_and_flat_verified=False,
    )) is None


def test_delivery_handles_unconfigured_no_network(monkeypatch):
    class Forbidden:
        def __init__(self,*args,**kwargs):
            raise AssertionError("NETWORK_CLIENT_MUST_NOT_START")
    monkeypatch.setattr(notices,"T100LiveOpsNotifier",Forbidden)
    event=notices.OpsNotice("k","title","desc","warning")
    assert asyncio.run(notices.send_v40_notice({},event)) is False


def test_delivery_uses_redacted_allowlisted_hook(monkeypatch):
    seen=[]
    class Sender:
        def __init__(self,url,*,cooldown_seconds):
            seen.append(("opened",url==HOOK))
        async def send(self,event):
            seen.append(("event",event.key))
            return True
        async def close(self):
            seen.append(("closed",True))
    monkeypatch.setattr(notices,"T100LiveOpsNotifier",Sender)
    e=notices.OpsNotice("v40-606","title","desc","warning")
    assert asyncio.run(notices.send_v40_notice(
        {"T100_V40_DISCORD_WEBHOOK_URL":HOOK},e,
    )) is True
    assert seen==[("opened",True),("event","v40-606"),("closed",True)]


def test_live_scanner_emits_only_genuinely_verified_stage2(monkeypatch):
    received=[]
    observed=scanner.V40ObservedSignal(
        signal_id=606,symbol="PIPPIN_USDT",tier="HIGH_225",
        p2_at=T,pre24_vol_pct=28.4513,
    )
    result=scanner.V40ShadowBatch(
        status="V40_SHADOW_FRESH_STAGE2_CAUSAL_VOL30_AUDIT_NONAUTHORIZING",
        validated_stage2=1,vol30_passed_research_only=1,
        passed_signals=(observed,),history_verified=True,
        source_readonly_transaction=True,
    )
    async def noop(*args,**kwargs):
        return None
    async def inspect(*args,**kwargs):
        return result
    async def capture(env,notice):
        received.append(notice)
        return True
    monkeypatch.setattr(scanner.T100Scanner,"process_eval",noop)
    monkeypatch.setattr(scanner,"inspect_new_v40_stage2",inspect)
    monkeypatch.setattr(scanner,"send_v40_notice",capture)
    obj=object.__new__(scanner.V40Stage2RenderScanner)
    obj.db=SimpleNamespace(pool=object())
    asyncio.run(obj.process_eval(T))
    assert len(received)==1
    assert received[0].key=="v40-stage2-606"


def test_live_trader_emits_error_once_then_skips_signal(monkeypatch):
    received=[]
    count=0
    async def cycle(env,*,rejected_signal_ids=frozenset()):
        nonlocal count
        count+=1
        if count==1:
            return worker.V40CanaryWorkerStatus(
                "V40_FIRST_CANARY_CURRENT_RISK_DEPTH_OR_ACCOUNT_HALT",
                signal_id=606,precheck_failure_code="V40_CANARY_RISK_MARKET_WITNESS_STALE",
            )
        if count==2:
            assert rejected_signal_ids==frozenset({606})
            return worker.V40CanaryWorkerStatus(
                "V40_FIRST_CANARY_REJECTED_SIGNAL_NO_RETRY",signal_id=606,
            )
        return worker.V40CanaryWorkerStatus(
            "V40_FIRST_CANARY_WAIT_FOR_GENUINE_STAGE2",
        )
    async def pause(seconds):
        pass
    async def capture(env,notice):
        received.append(notice)
        return True
    monkeypatch.setattr(worker,"send_v40_notice",capture)
    monkeypatch.setattr(worker,"validate_live_worker_config",
                        lambda env,review=None:worker.V40CanaryWorkerStatus(
                            "V40_REAL_WORKER_RELEASE_CONFIG_VERIFIED_NEEDS_CURRENT_SIGNED_EVIDENCE",
                            release_review_complete=True,
                        ))
    assert asyncio.run(worker.run_live_worker(
        {},max_cycles=3,cycle=cycle,pause=pause,
    )) == 0
    assert count==3
    assert len(received)==1
    assert received[0].severity=="critical"
