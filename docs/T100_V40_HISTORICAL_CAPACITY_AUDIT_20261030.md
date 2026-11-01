# T100/V40 historical signal-time capacity audit (offline, 2026-10-30)

**Release status: RESEARCH ONLY / NOT AUTHORIZED FOR TRADING.** All modules in
this research path remain unconnected to the order adapter. No MEXC API,
credentials, Render services, databases, DB migrations or BOXWAVE code are used.

## Scope and critical finding

The frozen V40 admission strategy models up to four independent slots.
The HIGH slot target is 62.5% of equity *before* frozen shock, same-candle
turnover, H4 stressed EXIT capacity, and contract-lot corrections.
For HYBRID/KNOWN_ONLY, exported `notional` may already include the
historical lot clipping. It must NOT be used as evidence that the
**unclipped pre-lot reference notional** fits current/public venue limits.

`app/t100_live_v40_historical_capacity.py` reconstructs for each actual
exported entry:

```
pre_lot_reference = min(
    desired_before_liquidity,
    prior_turnover_quota_usdt,
    0.05 * p25_turnover,
    exit_capacity_limit,
)
```

It rejects an entry record if its exported notional exceeds this reference
beyond the existing parity tolerance. It then evaluates the pre-lot reference
against its own archived price and public contract metadata; no silent
downsizing, synthetic fill, post-entry time window or order splitting.

## Frozen archive contract

`scripts/v40_historical_capacity_audit.py` reads the immutable
`~/v40_parity_for_live.zip` without extracting it and verifies its SHA-256
against the source-audited reference:

```
2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5
```

This is the reference from `docs/V40_HISTORICAL_EXPORT_PARITY_20261023.md`.
A different ZIP is **blocked**; a test-only library argument permits fictional
miniature fixtures. Archive identity is not sufficient to prove historical
contract specifications or exchange execution capacity.

The default scenario is **$10,000 / 100 bps / HYBRID**; options allow
$2k/$10k/$100k, 100/200 bps, and REFERENCE/HYBRID/KNOWN_ONLY.

## Run without historical contract evidence

```bash
T100_EXECUTION_MODE=paper T100_LIVE_ORDER_API_ENABLED=false \
T100_LIVE_KILL_SWITCH=true \
python3 scripts/v40_historical_capacity_audit.py \
  ~/v40_parity_for_live.zip --capital 10000 --cost-bps 100 --mode HYBRID
```

This first run **should report historical point-in-time evidence missing**
unless an explicit dated sidecar is provided. Neither that status nor a
historical capacity rejection is a request to send orders.

Optional `--template-output /local/path/entry_witness_template.jsonl`
writes one skeletal witness for each exported entry. Files are written only
on the local machine, never to GitHub.

## Required independently archived evidence

One JSON object per line in `--evidence-jsonl /local/path/witness.jsonl`,
keyed by exact exported `symbol` and timezone-aware `time`:

```json
{
  "symbol": "EXAMPLE_USDT",
  "time": "2025-06-01T12:00:00Z",
  "entry_reference_price": "0.123",
  "price_observed_at": "2025-06-01T11:59:00Z",
  "contract_observed_at": "2025-06-01T11:00:00Z",
  "contract": {
    "symbol": "EXAMPLE_USDT",
    "contractSize": "...",
    "volUnit": "...",
    "minVol": "...",
    "maxVol": "...",
    "riskLimitMode": "INCREASE",
    "riskLimitType": "BY_VOLUME",
    "...": "all other authentic contemporaneous MEXC public contract fields"
  }
}
```

This is a **schema illustration, not a valid historical MEXC record**.
The `contract` object must contain the complete contemporaneously archived
public metadata required by `assess_public_v40_capacity`, including risk
base/increments or full CUSTOM tiers, contract state, API eligibility,
leverage restrictions, maintenance and liquidation rates.

Witness dates must not be in the future relative to the signal. Contract
snapshots may be no more than 24h old, and prices no more than 30 minutes
old. Freshness alone does NOT prove the venue actually produced the
user-supplied values. No historical information is fabricated or fetched
from present-day MEXC endpoints.

The aggregate terminal report contains outcome counts only, not symbols,
prices, balances, order IDs, or credentials. Optional `--output-details`
writes a **local-only** per-entry report containing archived trading data.
Do not publish private archive entries or credentials.

## Known limits

- The historical archive does NOT independently supply point-in-time MEXC
  country contract-spec snapshots for every signal. Until those are
  provided, historical capacity classifications remain **UNKNOWN**.
- V40's prior-turnover and H4 exit assumptions are historical proxies, not
  live order-book executable depth.
- The public tier and max-order results are not current private account
  risk limits, cross-margin eligibility, account collateral, liquidation
  calculations, future tier maxima, or verified native-stop effectiveness.
- The scenario may already contain post-cap clipping in the exported
  actual entry. Original executed V40 performance cannot be promoted to
  a capacity-certified portfolio without an independently replayed,
  point-in-time, size-aware trade and exit path.
- `entry_allowed`, `live_authorization`, and `mexc_orders_sent` remain
  false under every outcome.
