from __future__ import annotations

from dataclasses import dataclass

STRATEGY_ID = "t100_200_stage2_p15_a4_sl75_trail14_gap0p5_v2"
PAPER_RUN_ID = "t100_200_stage2_p15_a4_sl75_trail14_gap0p5_shadow_v2"

# Frozen signal contract.
MIN_AMOUNT_24H = 3_000_000.0
HIGH_RISK_MIN_AMOUNT_24H = 500_000.0
MAX_SYMBOLS = 400
DISCOVERY_MIN_RETURN_24H = 0.05
DISCOVERY_MIN_CROSS_SECTION_PERCENTILE = 0.70
WIDE_SCAN_MIN_RETURN_72H = 0.20
EXCLUDED_SYMBOLS = frozenset({"BTC_USDT", "ETH_USDT"})

SHORT_EXHAUSTION_SCORE = 3
RETEST_TOLERANCE_ATR = 0.5
RETEST_WINDOW_30M = 3
REARM_NEW_HIGH_PCT = 0.05
ARMED_RUNNER_MEMORY_HOURS = 48
CONFIRMED_REARM_HOURS = 48
EPISODE_MAX_AGE_HOURS = 240
EMA_EQ_30_ALPHA = 0.36
ATR30_PERIOD = 7

# Frozen T100_200 Stage-1 gate / tiering.
STAGE1_MIN_RETURN_24H = 0.15
STAGE1_MIN_ATR7_PCT = 4.0
HIGH_TIER_MIN_ATR7_PCT = 5.7
LOW_TIER = "LOW_100"
HIGH_TIER = "HIGH_200"
LOW_NOTIONAL_FRACTION = 0.25
HIGH_NOTIONAL_FRACTION = 0.50
MAX_OPEN_POSITIONS = 4

# Frozen exits.
STOP_PCT = 75.0
TRAIL_ACTIVATION_PCT = 14.0
TRAIL_GAP_PCT = 0.5

# Frozen realistic-cost paper accounting.
FEE_RATE = 0.0008
SLIPPAGE_BPS = 25.0
SLIPPAGE_RATE = SLIPPAGE_BPS / 10_000.0

STABLECOIN_BASES = frozenset(
    {
        "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "USDP", "GUSD", "DAI",
        "FRAX", "LUSD", "USDD", "USDJ", "USDK", "UST", "USTC", "USDE",
        "SUSDE", "PYUSD", "CRVUSD", "RAI",
    }
)


@dataclass(frozen=True, slots=True)
class Stage1Gate:
    strict365: bool
    return_24h: float | None
    atr7_pct_entry: float | None

    @property
    def eligible(self) -> bool:
        return (
            self.strict365
            and self.return_24h is not None
            and self.return_24h >= STAGE1_MIN_RETURN_24H
            and self.atr7_pct_entry is not None
            and self.atr7_pct_entry >= STAGE1_MIN_ATR7_PCT
        )

    @property
    def tier(self) -> str | None:
        if not self.eligible or self.atr7_pct_entry is None:
            return None
        return HIGH_TIER if self.atr7_pct_entry >= HIGH_TIER_MIN_ATR7_PCT else LOW_TIER


def base_symbol(symbol: str) -> str:
    return symbol.upper().split("_", 1)[0]


def excluded_stablecoin_base(symbol: str) -> bool:
    return base_symbol(symbol) in STABLECOIN_BASES


def notional_fraction(tier: str) -> float:
    if tier == LOW_TIER:
        return LOW_NOTIONAL_FRACTION
    if tier == HIGH_TIER:
        return HIGH_NOTIONAL_FRACTION
    raise ValueError(f"unsupported T100_200 tier: {tier}")


def short_return_pct(entry: float, price: float) -> float:
    if entry <= 0 or price <= 0:
        raise ValueError("prices must be positive")
    return (entry - price) / entry * 100.0


def stop_price(entry: float) -> float:
    return entry * (1.0 + STOP_PCT / 100.0)


def trail_floor_pct(
    best_profit_pct: float,
    trail_gap_pct: float = TRAIL_GAP_PCT,
) -> float:
    return best_profit_pct - trail_gap_pct


def trail_trigger_price(
    entry: float,
    best_profit_pct: float,
    trail_gap_pct: float = TRAIL_GAP_PCT,
) -> float:
    floor = trail_floor_pct(best_profit_pct, trail_gap_pct)
    return entry * (1.0 - floor / 100.0)


def trail_reason(trail_gap_pct: float) -> str:
    label = f"{trail_gap_pct:g}".replace(".", "p")
    return f"trail_gap{label}"


@dataclass(frozen=True, slots=True)
class BarExitDecision:
    exit_price: float | None
    reason: str | None
    trail_active: bool
    best_profit_pct: float


def evaluate_completed_bar(
    *,
    entry: float,
    high: float,
    low: float,
    trail_active: bool,
    best_profit_pct: float,
    trail_activation_pct: float = TRAIL_ACTIVATION_PCT,
    trail_gap_pct: float = TRAIL_GAP_PCT,
) -> BarExitDecision:
    """Apply the frozen ADVERSE_FIRST Min30 exit chronology.

    Stops/trailing levels that existed before the bar are checked against the
    adverse high first. Only if no pre-existing adverse exit fires do we credit
    the favorable low, arm/update the trail, and carry that new level forward to
    the next completed bar. A trail armed by this bar therefore cannot exit in
    the same bar.
    """
    if min(entry, high, low) <= 0:
        raise ValueError("prices must be positive")

    if trail_active:
        trigger = trail_trigger_price(entry, best_profit_pct, trail_gap_pct)
        if high >= trigger:
            return BarExitDecision(
                trigger,
                trail_reason(trail_gap_pct),
                True,
                best_profit_pct,
            )
    else:
        catastrophic = stop_price(entry)
        if high >= catastrophic:
            return BarExitDecision(catastrophic, "sl75", False, best_profit_pct)

    favorable = short_return_pct(entry, low)
    updated_best = max(best_profit_pct, favorable)
    updated_active = trail_active or updated_best >= trail_activation_pct
    return BarExitDecision(None, None, updated_active, updated_best)
