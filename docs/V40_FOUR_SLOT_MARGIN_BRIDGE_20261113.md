# V40 four-slot historical-margin feasibility witness — 2026-11-13

**Status: feature branch only; offline/non-authorizing; not deployed.**

## Scope and provenance

This change adds \`app/t100_v40_unreleased_four_slot_margin_bridge.py\`.
It consumes the output of the existing pure historical admission/lot preview
(\`app/t100_v40_historical_margin_preview.py\`) and compares one to four
lot-quantized SHORT notionals against a *hypothetical*, explicitly supplied
CROSS collateral balance and an observed leverage value.

The reported frozen $2,000 V40 historical scenario finished around $14,476.81
(7.24x, 108 entries, historical peak gross ~209%, high-envelope drawdown
-54.87%). These **are reference claims from the archive**, not produced or
re-certified by this new witness.

The witness deliberately does **not** replay the historical trade sequence,
apply sequential shock-budget deductions for a real portfolio, authenticate
MEXC available collateral, reproduce exchange maintenance tiers, or establish
where liquidation would occur.

## Findings from unclipped illustrative targets

For equity $2,000, four simultaneous theoretical HIGH targets ($1,250 each)
give $5,000 notional, or 250% gross exposure. At observed 1x nominal initial
margin is $5,000, exceeding $2,000 collateral by $3,000. At observed 5x,
nominal initial margin is $1,000, but a *joint hypothetical* +75% move
against all shorts loses $3,750 (187.5% of equity), prior to funding,
fees, maintenance margin or liquidation.

These **are not actual four historical fills**. The original causal VOL30/H4,
P10/P25 turnover, shock-budget and contract-lot floors can reduce notional.
A research **5x pooled shock-budget proxy** is not evidence of an exchange
5x leverage configuration, and changing live leverage does not make the
original backtest identical or stop-safe.

## Invariants

- Accept only 1–4 unique symbols and immutable, nonauthorizing ADMIT plans.
- Reject nonfinite collateral/equity/notionals, mismatched leverage, >4 slots,
  size > desired/model, or nominal margin inconsistent with plan size.
- Quantized portfolio notional must be calculated from the precomputed
  historical admission plans; the bridge does not silently shrink it to the
  $12.50 pilot cap or force a 5x exchange setting.
- Compare nominal initial margin to the provided available collateral.
  A second *illustrative* SL75 joint-price-shock scenario is reported.
- No branch can set \`entry_allowed\`, \`live_authorization\`,
  \`liquidation_safety_certified\`, \`maintenance_margin_verified\`, or
  perform MEXC POST, exchange configuration mutation, or DB write.
- A nominal-margin or stress check passing **never** proves exchange-native
  liquidation safety or historic/live execution equivalence.

## Tests and release gate

\`tests/test_t100_v40_unreleased_four_slot_margin_bridge.py\` checks
high-tier 1x insufficiency, 5x nominal-margin-but-not-stress sufficiency,
four LOW slots versus the one-position canary, a single small cap-fitting
plan, and fail-closed malformed data. The feature-branch GitHub workflow runs
these tests together with the preexisting historical margin preview tests.

Before any deployment, separately verify **with real read-only exchange
evidence** account-wide gross inventory, available CROSS collateral,
MEXC leverage and maintenance tiers, actual contract sizes, native-stop
semantics and liquidation risk. Then replay the exact frozen sequential
V40 ledger across historical entry, trailing, stop, funding, fees,
and portfolio admission decisions. Finally complete full four-position
runtime/reservation/protection and independently resolve the guardian
critical latch. None of those steps is certified by this change.

The currently running trader/guardian/scanner are unchanged. The single
$12.50 live canary profile is **not equivalent** to the historical
four-slot portfolio and remains nonauthorizing.
