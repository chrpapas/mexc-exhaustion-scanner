"""Independent prior-close risk reduction model; research only, no orders."""
from decimal import Decimal as D


def planned_trims(equity, marked_by_symbol, cost_bps):
    """Return requested fractions before opening prices or pending fills.

    Frozen research assumptions: 20% shock, 5x pooled margin proxy, 5%
    reserve, 0.2% headroom, 8bps fee plus scenario cost. Not venue risk.
    """
    equity = D(str(equity))
    marked = {symbol: D(str(amount)) for symbol, amount in marked_by_symbol.items()}
    if not equity.is_finite() or any(not v.is_finite() or v < 0 for v in marked.values()):
        raise ValueError('INVALID_RISK_SNAPSHOT')
    if type(cost_bps) is not int or cost_bps not in (100, 200):
        raise ValueError('UNSUPPORTED_COST')
    gross = sum(marked.values(), D(0))
    coefficient = D('.20') + D('1.20')/(D(5)*D('.95'))
    costs = D('.0008') + D(cost_bps)/10000
    reduction = gross if equity <= 0 else min(gross, max(D(0),
        (coefficient*gross-equity*D('.998'))/(coefficient-costs)))
    plans = {}
    for symbol, amount in sorted(marked.items(), key=lambda item: (-item[1], item[0])):
        if amount > 0 and reduction > D('1e-8'):
            fraction = min(D(1), reduction/amount)
            plans[symbol] = fraction
            reduction -= amount*fraction
    return plans
