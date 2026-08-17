from datetime import UTC, datetime, timedelta

from app.signal_ledger import build_signal_ledger, signal_ledger_csv
from app.signal_ledger_table import render_signal_ledger_tables


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



def test_ledger_excludes_extreme_risk_from_csv_tracking():
    ledger = build_signal_ledger([
        _row(symbol="SAFE_USDT", risk_tier="standard"),
        _row(symbol="HIGH_USDT", risk_tier="high_risk"),
        _row(symbol="EXTREME_USDT", risk_tier="extreme_risk"),
    ], generated_at=datetime.now(UTC))
    text = signal_ledger_csv(ledger).decode("utf-8")
    assert ledger.total == 2
    assert "SAFE_USDT" in text
    assert "HIGH_USDT" in text
    assert "EXTREME_USDT" not in text

def test_visual_table_renderer_produces_png_with_risk_pages():
    confirmed = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    rows = [
        _row(symbol="SAFE_USDT", risk_tier="standard", target_20_at=confirmed + timedelta(hours=20)),
        _row(symbol="HIGH_USDT", risk_tier="high_risk", return_24h_pct=-0.10),
        _row(
            symbol="BOOM_USDT",
            risk_tier="extreme_risk",
            isolated_100_breach_at=confirmed + timedelta(hours=10),
            return_24h_pct=-1.20,
        ),
    ]
    ledger = build_signal_ledger(rows, generated_at=confirmed + timedelta(days=2))
    images = render_signal_ledger_tables(ledger, timezone_name="Europe/Zurich", rows_per_page=16)
    assert ledger.total == 2
    assert [item.risk_tier for item in images] == ["standard", "high_risk"]
    assert all(item.png_bytes.startswith(b"\x89PNG\r\n\x1a\n") for item in images)
    assert all(len(item.png_bytes) > 1000 for item in images)
