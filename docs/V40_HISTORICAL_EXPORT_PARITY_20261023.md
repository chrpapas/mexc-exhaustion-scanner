# V40 VOL30_SKIP+H4: historical admission parity audit

**Status: PASS for exported admission decisions and reference-mode liquidity sizing only.**

Checked archive SHA-256: `2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5`

## Coverage

| Check | Result |
|---|---:|
| Candidate signals | 146 |
| Strictly prior 30-minute candles | 7,154 (49 per signal) |
| Missing historical feature histories | 0 |
| Saved VOL30 gate rows compared | 2,376 |
| Entry diagnostic fields compared | 7,560 |
| Exact `REFERENCE` notional reconstructions | 648 / 648 |
| Total mismatches | **0** |

All 3 initial capitals ($2,000 / $10,000 / $100,000), both 100- and 200-bps slippage assumptions, and all three lot-handling modes (`REFERENCE`, `HYBRID`, `KNOWN_ONLY`) were included.

## Observed HYBRID scenario (100 bps / side)

| Starting capital | Saved final equity | Entries | Gate evaluated | Gate skipped | Max historical drawdown |
|---:|---:|---:|---:|---:|---:|
| $2,000 | $14,476.81 | 108 | 132 | 24 | -54.87% |
| $10,000 | $44,210.74 | 108 | 132 | 24 | -36.81% |
| $100,000 | $197,724.20 | 108 | 131 | 23 | -22.62% |

**How entry notional was verified:** for each `REFERENCE` entry, the independent auditor reconstructed `min(desired_before_liquidity, remaining_same_bar_quota, 5% × causal P25 turnover, H4 stressed exit-capacity)` using the supplied prior candles and saved pre-liquidity request. All 648 exported reference amounts matched within 0.000003 USDT plus a small relative tolerance. This is an arithmetic and admission-capacity check; it does not independently reconstruct preceding portfolio equity or funding.

## Explicit limitations

- The **full historical portfolio, funding, exits, slippage and account path were not independently replayed**; reported ending equity is from the existing exported V40 metrics, not an auditor recomputation.
- HYBRID and KNOWN_ONLY lot sizing cannot be independently reconstructed from this export without each historical/current contract-specification snapshot; only their target, turnover and capacity diagnostic fields were checked.
- The 146 candidate signals were extracted from the original V40 dataset, but only candidates reaching the admission volatility gate have a gate row. The difference (14 for $2k/$10k, 15 for $100k) reflects upstream position-state decisions.
- **H4** is a four-bar *stressed exit capacity bound*, **not** a 120-minute entry order window; same-bar child-order fragments in V39.1 are not verified historical exchange fills.
- The V40 equity-shock model uses a five-times nominal leverage proxy, which differs from the current draft live gateway’s isolated 1× cap.
- Real MEXC market orders, native protective stops, partial-fill handling, account reconciliation and a live scheduler remain unverified. No real orders were submitted.

## Reproduction

From a checkout with `app/t100_v40_admission.py` available:

```bash
PYTHONPATH=. python scripts/v40_historical_parity_audit.py ~/v40_parity_for_live.zip --output ~/v40_parity_audit.json
```

No confidential raw history or user-provided candidate data is committed to GitHub. This audit report publishes only aggregate checks.