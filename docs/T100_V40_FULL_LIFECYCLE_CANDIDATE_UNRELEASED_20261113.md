# V40 full lifecycle candidate — 13 Nov 2026 — UNRELEASED

This branch is **not production**. Do not merge, deploy, flip the global
native-stop mutation status, run the 041 migration, or change any Render
variables until the **existing first genuine trade** has been verified.
Nothing in this branch is imported by the running Render worker.

## Strategy contract (not old TP5)
- Frozen V40 Stage2 + VOL30 + H4 admission, at most four slots with
  conditional LOW/HIGH dynamic target and historical liquidity clipping.
- SHORT, initial SL at entry +75% (venue-native protection).
- No fixed TP1/TP2/TP5 close.
- At 14% *favorable price movement*, begin trailing; retain best-ever
  favorable percentage; trailing stop = entry x (1 - (best - 0.5)/100).
- Strict completed Min30 adverse-first ordering: an EXISTING stop hit in
  the current bar takes priority over the favorable low; a newly armed
  level is first effective after that bar and independently verified
  stop amendment. A completed candle is not proof that an actual MEXC
  native order filled at its modeled stop price.
- Optional +5% Discord notice is informational, not an actual take-profit.

## Implemented in this isolated branch
- `app/t100_v40_unreleased_exit_policy.py`: typed chronological candle
  projection, initial SL75, trailing activation and tick-aligned proposals.
  Rejects missing/partial candles, pre-fill candles, unverified checkpoints,
  crossed proposed stops, and out-of-order 30m history. It never sends orders.
- `app/t100_v40_unreleased_trade_events.py`: distinct yellow/green/red/
  blue/purple/orange/gray Discord embeds and typed source-proof categories;
  stable event identities and a durable SQL outbox interface. No claim
  that a webhook ACK itself proves the exchange order.
- `app/t100_v40_unreleased_trade_dispatcher.py`: single-instance
  notification-only delivery helper, no MEXC APIs.
- `migrations/041_v40_trade_event_outbox_UNRELEASED.sql`: immutable
  event fields with no runtime grants and no automatic application.
- Regression tests covering modeled exit chronology and event semantics.

## Still required before full-V40 release
1. Verify first LIVE short entry, MEXC-native SL75 attachment, exact
   fills and exit provenance; separately prove emergency-close flow.
2. Complete venue-specific native MEXC stop amendment contract
   verification in SHORT CROSS mode. The production constant
   `LIVE_NATIVE_STOP_AMENDMENT_STATUS` must remain blocked until this
   is independently certified.
3. Wire a separately fenced nonflat supervisor that loads **signed fill
   timestamps**, causal continuous post-fill lows, durable checkpoints and
   a monotonically advancing 30m bar cursor. Amend stop once using the
   existing `StopMutationExecutor`; reconcile UNKNOWN via signed GET,
   never repeat a possibly submitted POST.
4. Connect exchange-verified stops and closed-position/fee/funding fills to
   the notification outbox, then provision an independent limited writer
   and Discord webhook without leaking it to logs. Ensure guardian incidents
   remain independent of business notifications.
5. Implement/review full four-slot V40 sizing/risk/leverage/turnover and H4
   stress-capacity matching the frozen archive, not the 12.50-USDT 1x
   first-canary override. No automatic escalation after the first trade.
6. Execute offline tests and CI, historical entry+exit chronological parity
   against the pinned frozen archive, 30m gap/restart drills, genuine
   exchange amendment tests, duplicate/retry/ambiguous-POST exercises,
   and ensure no false successful Discord messages.
7. Require explicit operator authorization for full V40 deployment,
   including a separately reviewed risk and liquidation model.

## Nonparity disclaimer
The historical 7.24x observed $2k HYBRID export was **not** an independent
end-to-end portfolio, fees, funding, fills and exit replay. This branch
does not certify or promise that return. The existing 12.50-USDT pilot
is an exchange implementation trial, not the full backtested strategy.

## Operational immutability
- Do not deploy this branch or run its migration.
- Do not change `mexc-standard-short-trader`, guardian, scanner or DB.
- Do not touch BOXWAVE.
- Avoid a fabricated Stage2 signal or synthetic MEXC order.
