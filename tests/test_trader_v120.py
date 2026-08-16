import pytest

from app.mexc_trade import MexcTradeClient
from app.trader_config import TraderSettings
from app.trader_logic import newly_breached_thresholds, protected_profit_floor_pct, short_price_for_return


def _clear(monkeypatch):
    keys = [
        "TRADING_MODE", "TRADER_MARGIN_MODE", "TRADER_ALLOWED_RISK_TIERS",
        "TRADER_MAX_OPEN_POSITIONS", "TRADER_SLOT_ALLOCATION_PCT",
        "TRADER_MAX_TOTAL_EXPOSURE_PCT", "TRADER_MAX_HIGH_RISK_POSITIONS",
        "TRADER_PROFIT_TARGET_PCT", "TRADER_PROTECTION_ARM_PCT",
        "TRADER_TRAIL_CALLBACK_PCT", "MEXC_BASE_URL",
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
    assert s.slot_allocation_pct == pytest.approx(3.333333)
    assert s.max_total_exposure_pct == 20
    assert s.max_high_risk_positions == 5
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
    assert s.max_high_risk_positions == 3


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
