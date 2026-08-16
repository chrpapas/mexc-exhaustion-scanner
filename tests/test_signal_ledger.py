from datetime import UTC, datetime, timedelta

from app.signal_ledger import build_signal_ledger, signal_ledger_csv


def _row(**overrides):
    confirmed = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    row = {
        "episode_id": 1,
        "symbol": "TEST_USDT",
        "confirmed_at": confirmed,
        "entry_price": 1.0,
        "risk_tier": "standard",
        "current_return_pct": -0.05,
        "first_profit_at": None,
        "target_20_at": None,
        "isolated_100_breach_at": None,
        "adverse_200_breach_at": None,
        "adverse_300_breach_at": None,
        "cross_400_breach_at": None,
        "return_24h_pct": 0.10,
        "return_48h_pct": -0.20,
        "return_72h_pct": None,
        "return_168h_pct": None,
    }
    row.update(overrides)
    return row


def test_ledger_reconstructs_horizon_prices_and_target_time():
    confirmed = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    ledger = build_signal_ledger([
        _row(target_20_at=confirmed + timedelta(hours=30))
    ], generated_at=confirmed + timedelta(days=4))
    item = ledger.items[0]
    assert item.time_to_target_20_hours == 30.0
    assert item.horizons[0].price == 0.9
    assert item.horizons[1].price == 1.2
    assert item.headline_status == "target_hit"


def test_breach_before_target_is_catastrophic_but_later_breach_is_not_target_failure():
    confirmed = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    first = build_signal_ledger([
        _row(
            target_20_at=confirmed + timedelta(hours=40),
            isolated_100_breach_at=confirmed + timedelta(hours=20),
            adverse_200_breach_at=confirmed + timedelta(hours=25),
        )
    ], generated_at=confirmed + timedelta(days=4)).items[0]
    assert first.target_before_100_breach is False
    assert first.headline_status == "breach_200"

    later = build_signal_ledger([
        _row(
            target_20_at=confirmed + timedelta(hours=20),
            isolated_100_breach_at=confirmed + timedelta(hours=40),
        )
    ], generated_at=confirmed + timedelta(days=4)).items[0]
    assert later.target_before_100_breach is True
    assert later.headline_status == "target_then_breach"


def test_csv_contains_all_requested_horizons_and_breach_columns():
    ledger = build_signal_ledger([_row()], generated_at=datetime.now(UTC))
    text = signal_ledger_csv(ledger).decode("utf-8")
    assert "signal_price" in text
    assert "price_1d" in text
    assert "return_1d_pct" in text
    assert "price_7d" in text
    assert "breach_100_at_utc" in text
    assert "breach_200_at_utc" in text
    assert "breach_300_at_utc" in text
    assert "breach_400_at_utc" in text
