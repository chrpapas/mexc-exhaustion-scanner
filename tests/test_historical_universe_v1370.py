from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.historical_universe import (
    UniverseConfig,
    _article_links,
    _symbols_from_csv,
    extract_futures_symbols,
)


def _config(tmp_path: Path, **overrides):
    values = dict(
        cache_dir=tmp_path,
        start=datetime(2026, 3, 1, tzinfo=UTC),
        end=datetime(2026, 9, 1, tzinfo=UTC),
    )
    values.update(overrides)
    return UniverseConfig(**values)


def test_universe_web_rate_is_hard_capped(tmp_path):
    with pytest.raises(ValueError):
        _config(tmp_path, requests_per_second=0.51).validate()


def test_extracts_explicit_and_grouped_crypto_symbols():
    raw = """
    <html><head><title>Delisting of WHITEWHALE and HIGH USDT-M Perpetual Futures Pairs [Jul 20, 2026]</title></head>
    <body><h1>Delisting of WHITEWHALE and HIGH USDT-M Perpetual Futures Pairs</h1>
    MEXC will be delisting the WHITEWHALE and HIGH USDT-M Perpetual Futures pairs on Jul 20, 2026, 08:00 (UTC).
    </body></html>
    """
    symbols, classification, observed, _title = extract_futures_symbols(raw)
    assert classification == "crypto_or_unknown"
    assert "WHITEWHALE_USDT" in symbols
    assert "HIGH_USDT" in symbols
    assert observed is not None and observed.year == 2026 and observed.month == 7


def test_stock_and_index_announcements_are_excluded():
    stock = "<title>MEXC to Delist ACN and BBAI USDT-M Stock Futures</title><body>#Stocks Stock Futures ACNUSDT BBAIUSDT</body>"
    symbols, classification, _observed, _title = extract_futures_symbols(stock)
    assert classification == "stock"
    assert symbols == set()

    index = "<title>MEXC to Delist SOXSUSDT Index Futures</title><body>SOXSUSDT Index Futures</body>"
    symbols, classification, _observed, _title = extract_futures_symbols(index)
    assert classification == "index"
    assert symbols == set()


def test_article_link_parser_deduplicates_and_absolutizes():
    raw = '<a href="/announcements/article/a-1">A</a><a href="https://www.mexc.com/announcements/article/a-1">A2</a>'
    assert _article_links(raw, "https://www.mexc.com") == ["https://www.mexc.com/announcements/article/a-1"]


def test_signal_csv_symbols_are_recovered(tmp_path):
    path = tmp_path / "signals.csv"
    path.write_text("id,symbol\n1,PONS_USDT\n2,USELESS_USDT\n3,bad\n", encoding="utf-8")
    assert _symbols_from_csv(path) == {"PONS_USDT", "USELESS_USDT"}


def test_module_is_db_independent():
    import app.historical_universe as module
    source = Path(module.__file__).read_text()
    assert "from app.db" not in source
    assert "from app.trader_db" not in source
    assert "DATABASE_URL" not in source
