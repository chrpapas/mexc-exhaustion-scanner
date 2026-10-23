# T100 live readiness: evidence and release gates

**Status: BLOCKED — draft branch only. No Render deployment, no real orders.**

## Verified offline evidence
- V40 exported historical parity: 146 candidates; 2,376 gate rows,
  7,560 entry diagnostic comparisons, 648 exact REFERENCE lot notionals.
  These checks do not certify HYBRID/KNOWN_ONLY lot snapshots or a full
  independent equity/exit/funding replay.
- Dedicated offline CI covers admission, short trailing, read-only order
  lifecycle, stop journal, restart recovery, FIFO short accounting,
  strict timestamp/pagination inventory snapshot validation, and
  rejection of unexpected native stop orders.
- CI is an offline correctness check; it is not a MEXC API certification.

## Release blockers (all mandatory)
1. **Venue contract:** pin official MEXC API specification and verify every
   private read field against actual account responses in read-only mode.
   Cover order state codes, position IDs, side/hedge mode, stop plan IDs,
   contract sizes, lot rounding, timestamps and pagination.
2. **Historical realism:** independently reproduce portfolio exposure,
   execution, stop/trail events, fees/funding, and adverse gap behavior
   using point-in-time contract specs. Backtest modeled equity is not
   exchange-realized equity.
3. **Trade completeness:** build a paginated, restart-safe trade and
   funding ingestion ledger with stable venue IDs and exact ordering.
   The offline accounting calculator must not be fed guessed fills,
   incomplete pages, or synthetic funding completeness flags.
4. **Protective stop lifecycle:** implement and review idempotent native
   stop create/modify/cancel with persistent write-ahead state, atomic
   compare-and-swap, timeout/unknown-result recovery and post-write
   exchange verification. A read-only proposed amendment does not
   protect a live position.
5. **Account-wide reconciliation:** support non-flat restart recovery,
   duplicate or partially filled orders, manual intervention,
   liquidation/margin drift, and protection against unrelated orders.
6. **Operational controls:** independent kill switch, risk caps,
   API-permission checks, clock skew, stale books, alerts, restart
   health, on-call response and verified emergency flatten procedure.
7. **Shadow exercise:** use only read-only exchange credentials and
   paper/synthetic execution, replay failures, and collect sustained
   evidence for parity, alerting and recoverability.
8. **Independent review and explicit operator authorization:** only
   after all blockers are evidenced, request separate approval for a
   tiny limited-risk live pilot. Do not interpret 'go' or 'continue'
   as permission to place funded orders.

## Current architecture boundaries
- `app/t100_live_guards.py` blocks the production paper portfolio
  from switching to live mode.
- `app/t100_live_lifecycle_audit.py` and
  `app/t100_live_recovery.py` perform read-only safety checks.
- `app/t100_live_stop_journal.py` persists stop intent and verifies
  observed native stop state; it **does not** issue amendments.
- `app/t100_live_shadow.py` reconciles one atomic, independently supplied
  inventory snapshot against managed short positions and native stops;
  returns SHADOW_ONLY and data-only trailing proposals, never orders.
  It is not a running service or an authenticated venue read.
- `app/t100_live_snapshot.py` validates independently supplied read-only
  inventory snapshots and rejects stale, incomplete or malformed reads;
  it does **not** itself establish pagination completeness or fetch venue data.
- `app/t100_live_accounting.py` computes deterministic FIFO realized
  short PnL for explicitly complete, ordered, linear USDT fills and
  funding events; it **does not** fetch or certify exchange history.
- `sql/t100_live_stop_amendments_draft.sql` is a **draft** schema, not
  an applied migration.

## What counts as ready
A green offline workflow alone is insufficient. The release requires
documented pass evidence for every gate above, a complete integration
test on actual read-only MEXC data, an operational shadow soak, and
separate explicit approval before any real order.
