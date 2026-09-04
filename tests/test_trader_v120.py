import pytest

from app.mexc_trade import MexcTradeClient
from app.trader_config import TraderSettings
from app.trader_logic import newly_breached_thresholds, protected_profit_floor_pct, short_price_for_return


def _clear(monkeypatch):
    keys = [
        "TRADING_MODE", "TRADER_EXECUTION_STRATEGY", "TRADER_PAPER_RUN_ID",
        "TRADER_TP5_TARGET_PCT", "TRADER_CATASTROPHIC_STOP_PCT", "TRADER_MARGIN_MODE", "TRADER_ALLOWED_RISK_TIERS",
        "TRADER_MAX_OPEN_POSITIONS", "TRADER_SLOT_ALLOCATION_PCT",
        "TRADER_MAX_TOTAL_EXPOSURE_PCT", "TRADER_MAX_STANDARD_POSITIONS", "TRADER_MAX_HIGH_RISK_POSITIONS",
        "TRADER_PROFIT_TARGET_PCT", "TRADER_PROTECTION_ARM_PCT",
        "TRADER_TRAIL_CALLBACK_PCT", "TRADER_STANDARD_HOLD_DAYS", "TRADER_HIGH_RISK_TIMEOUT_DAYS", "MEXC_BASE_URL",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)


def test_strategy_one_is_new_default(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    _clear(monkeypatch)
    s = TraderSettings.from_env()
    assert s.trading_mode == "paper"
    assert s.margin_mode == "cross"
    assert s.allowed_risk_tiers == ("STANDARD", "HIGH_RISK")
    assert s.max_open_positions == 6
    assert s.execution_strategy == "tp5_sl75_daily_core_persistence_skip_v1"
    assert s.slot_allocation_pct == pytest.approx(5.0)
    assert s.max_total_exposure_pct == 30
    assert s.tp5_target_pct == 5
    # Legacy tier caps remain configured for rollback but are ignored by TP5 generic slots.
    assert s.max_standard_positions == 5
    assert s.max_high_risk_positions == 1
    assert s.standard_hold_days == 7
    assert s.high_risk_timeout_days == 4
    assert s.profit_target_pct == 20
    assert s.protection_arm_pct == 25
    assert s.trail_callback_pct == 15
    assert s.paper_taker_fee_rate == pytest.approx(0.0008)


def test_deprecated_contract_rest_domain_is_upgraded(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("MEXC_BASE_URL", "https://contract.mexc.com")
    s = TraderSettings.from_env()
    assert s.mexc_base_url == "https://api.mexc.com"


def test_20_floor_then_price_trail():
    assert protected_profit_floor_pct(peak_profit_pct=24.99) is None
    assert protected_profit_floor_pct(peak_profit_pct=25) == pytest.approx(20)
    assert protected_profit_floor_pct(peak_profit_pct=30) == pytest.approx(20)
    assert protected_profit_floor_pct(peak_profit_pct=40) == pytest.approx(31)
    assert protected_profit_floor_pct(peak_profit_pct=60) == pytest.approx(54)
    assert short_price_for_return(100, 20) == pytest.approx(80)
    assert short_price_for_return(100, 54) == pytest.approx(46)


def test_adverse_breaches_are_cumulative_and_never_replace_earlier_levels():
    assert newly_breached_thresholds(max_adverse_pct=350, already_breached=set()) == [100, 200, 300]
    assert newly_breached_thresholds(max_adverse_pct=420, already_breached={100, 200}) == [300, 400]
    assert newly_breached_thresholds(max_adverse_pct=420, already_breached={100, 200, 300, 400}) == []


@pytest.mark.asyncio
async def test_live_order_uses_current_mexc_create_endpoint(monkeypatch):
    client = MexcTradeClient("https://api.mexc.com", api_key="k", api_secret="s")
    calls = []

    async def fake_post(path, body, **kwargs):
        calls.append((path, body))
        return {"orderId": "739113577038255616", "ts": 1}

    monkeypatch.setattr(client, "_private_post", fake_post)
    try:
        order_id = await client.submit_market_short(
            symbol="ABC_USDT", contracts=10, open_type=2, reference_price=1.23,
            external_oid="test", leverage=1,
        )
    finally:
        await client.close()
    assert order_id == 739113577038255616
    assert calls[0][0] == "/api/v1/private/order/create"
    assert calls[0][1]["side"] == 3
    assert calls[0][1]["openType"] == 2
    assert calls[0][1]["positionMode"] == 1


@pytest.mark.asyncio
async def test_live_protection_uses_position_stop_and_can_ratchet(monkeypatch):
    client = MexcTradeClient("https://api.mexc.com", api_key="k", api_secret="s")
    calls = []

    async def fake_post(path, body, **kwargs):
        calls.append((path, body))
        return "12345" if path.endswith("/place") else None

    monkeypatch.setattr(client, "_private_post", fake_post)
    try:
        stop_id = await client.place_position_stop(position_id=99, contracts=10, stop_price=0.8)
        await client.modify_position_stop(stop_order_id=stop_id, stop_price=0.69)
    finally:
        await client.close()
    assert stop_id == 12345
    assert calls[0][0] == "/api/v1/private/stoporder/place"
    assert calls[0][1]["stopLossPrice"] == pytest.approx(0.8)
    assert calls[1][0] == "/api/v1/private/stoporder/change_plan_price"
    assert calls[1][1]["stopLossPrice"] == pytest.approx(0.69)


def test_slot_defaults_follow_configured_capacity(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    _clear(monkeypatch)
    monkeypatch.setenv("TRADER_MAX_OPEN_POSITIONS", "4")
    monkeypatch.setenv("TRADER_MAX_TOTAL_EXPOSURE_PCT", "20")
    s = TraderSettings.from_env()
    assert s.slot_allocation_pct == pytest.approx(5.0)
    assert s.max_standard_positions == 3
    assert s.max_high_risk_positions == 1


def test_high_only_does_not_reserve_unused_standard_slot(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    _clear(monkeypatch)
    monkeypatch.setenv("TRADER_MAX_OPEN_POSITIONS", "4")
    monkeypatch.setenv("TRADER_ALLOWED_RISK_TIERS", "HIGH_RISK")
    s = TraderSettings.from_env()
    assert s.max_high_risk_positions == 4


@pytest.mark.asyncio
async def test_current_stop_order_query_uses_documented_endpoint(monkeypatch):
    client = MexcTradeClient("https://api.mexc.com", api_key="k", api_secret="s")
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return [{"id": 7, "positionId": 99, "state": 1}]

    monkeypatch.setattr(client, "_private_get", fake_get)
    try:
        rows = await client.open_stop_orders("ABC_USDT")
    finally:
        await client.close()
    assert rows[0]["id"] == 7
    assert calls == [("/api/v1/private/stoporder/open_orders", {"symbol": "ABC_USDT"})]


def test_scanner_watchdog_defaults(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.delenv("TRADER_WATCHDOG_STALE_SECONDS", raising=False)
    monkeypatch.setenv("DISCORD_TRADER_EVENTS_WEBHOOK_URL", "https://example.invalid/webhook")
    settings = Settings.from_env()
    assert settings.trader_watchdog_stale_seconds == 180
    assert settings.discord_trader_events_webhook_url.endswith("/webhook")
    assert settings.signal_eval_concurrency == 3
    assert settings.signal_eval_progress_every == 50

@pytest.mark.asyncio
async def test_high_signal_blocked_by_exposure_is_logged_persisted_but_not_discord_notified(monkeypatch, caplog):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    import sys
    import types

    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader
    from app.trader_models import TradeSignal

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    _clear(monkeypatch)
    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", "tier_v1")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)

    decisions = []
    notices = []
    active = [
        SimpleNamespace(
            symbol="VELVET_USDT", risk_tier="STANDARD", slot_no=1,
            notional_usdt=200.0, current_return_pct=0.0,
        )
    ]

    class Repo:
        async def active_positions(self):
            return active

        async def decision(self, signal_id, decision, reason, position_id=None):
            decisions.append((signal_id, decision, reason, position_id))

    trader.repo = Repo()

    async def fake_equity(_active=None):
        return 1000.0

    async def fake_notify(title, description, event_fields=None, *, color=None):
        notices.append((title, description, event_fields, color))

    monkeypatch.setattr(trader, "_account_equity", fake_equity)
    monkeypatch.setattr(trader, "_notify", fake_notify)
    caplog.set_level("INFO", logger="app.trader")

    signal = TradeSignal(
        id=240,
        symbol="PORTAL_USDT",
        signaled_at=datetime.now(UTC),
        episode_id=240,
        entry_hint=0.01754,
        risk_tier="HIGH_RISK",
        features={},
    )
    await trader._handle_signal(signal)

    assert decisions and decisions[0][1] == "ignored_exposure"
    assert "aggregate exposure cap reached" in decisions[0][2]
    assert notices == []
    assert "decision=IGNORED_EXPOSURE" in caplog.text
    assert "PORTAL_USDT" in caplog.text


@pytest.mark.asyncio
async def test_filtered_extreme_signal_is_logged_persisted_but_not_discord_notified(monkeypatch, caplog):
    from datetime import UTC, datetime
    import sys
    import types

    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader
    from app.trader_models import TradeSignal

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    _clear(monkeypatch)
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)

    decisions = []
    notices = []

    class Repo:
        async def active_positions(self):
            return []

        async def decision(self, signal_id, decision, reason, position_id=None):
            decisions.append((signal_id, decision, reason, position_id))

    trader.repo = Repo()

    async def fake_notify(title, description, event_fields=None, *, color=None):
        notices.append((title, description, event_fields, color))

    monkeypatch.setattr(trader, "_notify", fake_notify)
    caplog.set_level("INFO", logger="app.trader")

    signal = TradeSignal(
        id=239,
        symbol="BRIAN_USDT",
        signaled_at=datetime.now(UTC),
        episode_id=239,
        entry_hint=0.0005423,
        risk_tier="EXTREME_RISK",
        features={},
    )
    await trader._handle_signal(signal)

    assert decisions and decisions[0][1] == "ignored_risk"
    assert notices == []
    assert "decision=IGNORED_RISK" in caplog.text
    assert "BRIAN_USDT" in caplog.text
