from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.trader_config import RECOVERY_RUNNER_STRATEGY, TraderSettings
from tests.test_trader_v123 import _position


class RunnerRepo:
    def __init__(self, position):
        self.p = position
        self.paper_delta = 0.0
        self.partial_calls = 0
        self.protection_floors: list[float] = []

    async def update_market(self, position_id, *, price, return_pct, peak_profit_pct, max_adverse_pct, profit_floor_pct):
        self.p = replace(
            self.p,
            current_price=price,
            current_return_pct=return_pct,
            peak_profit_pct=peak_profit_pct,
            max_adverse_pct=max_adverse_pct,
            profit_floor_pct=profit_floor_pct,
        )
        return self.p

    async def mark_breach(self, *args, **kwargs):
        return False

    async def position(self, position_id):
        return self.p

    async def record_partial_close(
        self,
        position,
        *,
        exit_price,
        closed_quantity_base,
        remaining_quantity_base,
        exit_fee_usdt,
        metadata_patch,
    ):
        self.partial_calls += 1
        partial_pnl = closed_quantity_base * (position.entry_price - exit_price)
        metadata = dict(position.metadata)
        metadata.update(metadata_patch)
        self.p = replace(
            position,
            quantity_base=remaining_quantity_base,
            notional_usdt=remaining_quantity_base * position.entry_price,
            current_price=exit_price,
            current_return_pct=(position.entry_price - exit_price) / position.entry_price * 100.0,
            realized_pnl_usdt=position.realized_pnl_usdt + partial_pnl,
            exit_fee_usdt=position.exit_fee_usdt + exit_fee_usdt,
            metadata=metadata,
        )
        return self.p

    async def adjust_paper_equity(self, delta):
        self.paper_delta += delta
        return 1000.0 + self.paper_delta

    async def mark_protection_armed(self, position_id, *, order_id, floor_pct, price, return_pct):
        self.protection_floors.append(floor_pct)
        self.p = replace(
            self.p,
            protection_armed_at=datetime.now(UTC),
            profit_floor_pct=floor_pct,
        )
        return True

    async def set_protection(self, position_id, *, order_id, floor_pct):
        self.protection_floors.append(floor_pct)
        self.p = replace(self.p, profit_floor_pct=max(self.p.profit_floor_pct or -1e9, floor_pct))

    async def patch_metadata(self, position_id, patch):
        metadata = dict(self.p.metadata)
        metadata.update(patch)
        self.p = replace(self.p, metadata=metadata)


def runner_position(*, adverse=35.0, partial_done=False, floor=None, peak=0.0):
    metadata = {
        "tp_target_pct": 5.0,
        "execution_strategy": RECOVERY_RUNNER_STRATEGY,
        "original_notional_usdt": 100.0,
        "original_quantity_base": 100.0,
        "recovery_runner_enabled": True,
        "recovery_runner_adverse_pct": 30.0,
        "recovery_runner_fraction": 0.5,
        "recovery_runner_trail_gap_pct": 1.0,
        "recovery_runner_update_step_pct": 0.25,
        "recovery_runner_partial_done": partial_done,
    }
    return _position(
        risk_tier="STANDARD",
        exit_strategy="tp5_adv30_runner50_trail1",
        position_maturity="profit_5",
        entry_price=1.0,
        entry_equity_usdt=1000.0,
        notional_usdt=50.0 if partial_done else 100.0,
        quantity_base=50.0 if partial_done else 100.0,
        peak_profit_pct=peak,
        max_adverse_pct=adverse,
        profit_floor_pct=floor,
        protection_armed_at=datetime.now(UTC) if partial_done else None,
        metadata=metadata,
    )


def test_promoted_candidate_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    for key in ("TRADER_EXECUTION_STRATEGY", "TRADER_MAX_OPEN_POSITIONS", "TRADER_SLOT_ALLOCATION_PCT", "TRADER_MAX_TOTAL_EXPOSURE_PCT"):
        monkeypatch.delenv(key, raising=False)
    settings = TraderSettings.from_env()
    assert settings.execution_strategy == RECOVERY_RUNNER_STRATEGY
    assert settings.max_open_positions == 10
    assert settings.slot_allocation_pct == pytest.approx(10.0)
    assert settings.max_total_exposure_pct == pytest.approx(100.0)
    assert settings.uses_recovery_runner
    assert settings.uses_daily_core_skip
    assert settings.uses_daily_bull_persistence_v2_skip
    assert not settings.uses_catastrophic_stop
    assert settings.recovery_runner_adverse_pct == 30.0
    assert settings.recovery_runner_fraction == 0.5
    assert settings.recovery_runner_trail_gap_pct == 1.0


@pytest.mark.asyncio
async def test_recovery_runner_only_arms_after_30pct_prior_adverse(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    trader = PortfolioShortTrader(TraderSettings.from_env())
    repo = RunnerRepo(runner_position(adverse=29.9))
    trader.repo = repo
    closes: list[str] = []

    async def fake_close(position, price, reason):
        closes.append(reason)

    monkeypatch.setattr(trader, "_close", fake_close)
    await trader._monitor_position(repo.p, 0.95)
    assert closes == ["tp5_profit_target_5"]
    assert repo.partial_calls == 0


@pytest.mark.asyncio
async def test_recovery_runner_realizes_half_at_tp5_and_arms_one_point_trail(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    trader = PortfolioShortTrader(TraderSettings.from_env())
    repo = RunnerRepo(runner_position(adverse=35.0))
    trader.repo = repo

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(trader, "_notify", quiet)
    await trader._monitor_position(repo.p, 0.95)

    assert repo.partial_calls == 1
    assert repo.p.quantity_base == pytest.approx(50.0)
    assert repo.p.notional_usdt == pytest.approx(50.0)
    assert repo.p.metadata["recovery_runner_partial_done"] is True
    assert repo.p.realized_pnl_usdt == pytest.approx(2.5)
    expected_fee = 50.0 * 0.95 * 0.0008
    assert repo.p.exit_fee_usdt == pytest.approx(expected_fee)
    assert repo.paper_delta == pytest.approx(2.5 - expected_fee)
    assert repo.p.profit_floor_pct == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_recovery_runner_raises_floor_and_exits_on_retrace(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    trader = PortfolioShortTrader(TraderSettings.from_env())
    repo = RunnerRepo(runner_position(adverse=35.0, partial_done=True, floor=4.0, peak=5.0))
    trader.repo = repo
    closes: list[str] = []

    async def fake_close(position, price, reason):
        closes.append(reason)

    monkeypatch.setattr(trader, "_close", fake_close)
    await trader._monitor_position(repo.p, 0.94)  # +6% => floor rises to +5%
    assert repo.p.profit_floor_pct == pytest.approx(5.0)
    assert closes == []

    await trader._monitor_position(repo.p, 0.951)  # +4.9% => below +5% floor
    assert closes == ["recovery_runner_trailing_exit"]

@pytest.mark.asyncio
async def test_live_recovery_runner_closes_half_and_protects_exact_remainder(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.mexc_trade import ContractSpec
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    trader = PortfolioShortTrader(TraderSettings.from_env())
    position = replace(
        runner_position(adverse=35.0),
        mode="live",
        capital_strategy="cross_portfolio",
        mexc_position_id=99,
        metadata={
            **runner_position(adverse=35.0).metadata,
            "contracts": 100.0,
        },
    )
    repo = RunnerRepo(position)
    trader.repo = repo

    class FakeTickerStream:
        async def remove(self, symbol):
            return None

    class FakeMexc:
        def __init__(self):
            self.close_contracts = None
            self.stop_contracts = None
            self.stop_price = None
            self.position_polls = 0
            self.ticker_stream = FakeTickerStream()

        async def contract_spec(self, symbol):
            return ContractSpec(
                symbol=symbol,
                contract_size=1.0,
                vol_unit=1.0,
                min_vol=1.0,
                max_vol=1_000_000.0,
                api_allowed=True,
                position_open_type=3,
            )

        async def close_market_short(self, **kwargs):
            self.close_contracts = kwargs["contracts"]
            return 777

        async def order(self, order_id):
            return {"dealAvgPrice": 0.95, "takerFee": 0.01}

        async def open_positions(self, symbol=None):
            self.position_polls += 1
            if self.position_polls < 3:
                return []
            return [{"positionId": 99, "holdVol": 50.0}]

        async def place_position_stop(self, *, position_id, contracts, stop_price):
            assert position_id == 99
            self.stop_contracts = contracts
            self.stop_price = stop_price
            return 888

    fake_mexc = FakeMexc()
    trader.mexc = fake_mexc

    async def no_sleep(_seconds):
        return None

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr("app.trader.asyncio.sleep", no_sleep)
    monkeypatch.setattr(trader, "_notify", quiet)

    await trader._monitor_position(repo.p, 0.95)

    assert fake_mexc.close_contracts == pytest.approx(50.0)
    assert fake_mexc.position_polls == 3
    assert repo.p.quantity_base == pytest.approx(50.0)
    assert repo.p.notional_usdt == pytest.approx(50.0)
    assert repo.p.metadata["contracts"] == pytest.approx(50.0)
    assert repo.p.metadata["recovery_runner_partial_done"] is True
    assert repo.p.exit_fee_usdt == pytest.approx(0.01)
    assert fake_mexc.stop_contracts == pytest.approx(50.0)
    assert repo.p.profit_floor_pct == pytest.approx(4.0)
    # +4% short-return floor from entry 1.0 => stop price 0.96.
    assert fake_mexc.stop_price == pytest.approx(0.96)
