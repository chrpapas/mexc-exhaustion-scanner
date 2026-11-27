# V40 dedicated canary — guardian audit & historical margin parity (2026-11-13)

Status: **not released / no live margin changes**.

## Guardian audit / recovery
On 2026-11-13 16:22:22 UTC a sole signed GET inventory probe failed
at `AFTER_STOPS`. No unexpected position or exchanged order was observed.
A critical incident was persisted and delivered to Discord. Subsequent
independent account bookends repeatedly observed a flat account, but the
recovery scope 002 remained correctly latched.

`app/t100_v40_guardian_recovery_003.py` is a **one-time audited scope**,
NOT a clearing of a previously latched row: it requires all three previous
scopes' current status FLAT, exactly one delivered critical incident of
the accepted no-inventory-proof class in EACH, no pending alert, current
002 heartbeat fresh, plus two further signed all-account and separate
immutable-ledger flat bookends. Any missing evidence fails closed. No
fifth scope will be synthesized. The 003 scope must be fully audited
using live SQL/GET evidence before activation.

## V40 reference / current account mismatch
The reported V40 $2k HYBRID 100-bps/side scenario yielded $14,476.81
(7.24x), 108 entries and -54.87% historical max drawdown; admission
and reference-size decisions, NOT a full independent exits/funding replay,
were certified by the export audit.

Pure V40 reference admission:
- Four slots, low 25% equity target per slot, high 62.5% per slot;
  VOL30 prior-close volatility >=30% excluded.
- Target then capped by pooled 5x *historical shock budget*, causal P25
  previous-turnover 5% entry bound, P10 four-bar stressed exit capacity,
  30m bar quota, and contract-lot flooring.
- H4 means a four-bar exit-capacity constraint, not a 2h entry execution
  window.
- Model 5x is **NOT** an authenticated MEXC cross-leverage setting.
  Liquidation, maintenance tiers, exchange fees, funding, actual contract
  min-lots and native-stop execution cannot be inferred from it.
- The original SL75 and +14% 0.5pp trailing exit are not yet operational
  end to end in the current live canary worker.

At prior observed equity 56.37671495 USDT:
- LOW desired (before risk/liquidity/lot clipping): 14.0941787375 USDT.
- HIGH desired (before clipping): 35.23544684375 USDT.
- Current live per-order and total cap: 12.50 USDT; single position;
  CROSS 1x. Both unfloored historical targets exceed that cap.
- Changing leverage to 5x or increasing the cap without exchange-native
  SL/exit and full account reconciliation would **not** reproduce the
  7.24x backtest and could liquidate the account well before SL75.

`app/t100_v40_historical_margin_preview.py` compares original admission,
V39 integer lot flooring and expected initial exchange margin under an
**observed** leverage; it always returns live_order_allowed=false and
exchange_posts=0. Actual contract metadata and account risk must be
independently fetched and validated before live execution sizing.

## Rollout gate
1. Pass offline recovery and margin-parity CI.
2. Inspect actual SQL archived incident/outbox, new 003 creation result,
   MEXC account and guardian fresh signed GETs. If not verified, NO recovery.
3. Finish and test nonflat trailing/stop amendment, native close finalization,
   full four-slot reservations and account-wide margin/liquidation
   constraints. None may be bypassed with flags.
4. Explicitly separate the $12.50 one-shot canary from the hypothetical
   historical-risk live profile: do not silently switch from one to another.

The recovery patch and margin parity preview do not authorize or submit
any MEXC trades.
