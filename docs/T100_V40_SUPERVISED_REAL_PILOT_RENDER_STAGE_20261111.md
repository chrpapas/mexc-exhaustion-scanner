# T100 V40 supervised real-pilot Render staging — 11 November 2026

**Review-only. No MEXC order authorization, no live trade.**

## Verified runtime
- Render scanner service srv-d9kba2jm8hqs73c3dfi0 deployed on integration branch and operating.
- At 2026-11-11 12:05 UTC its 12:00 UTC scan reported 521 synchronized symbols (zero failures), 83 evaluated symbols, 0 eligible Stage2 and a successful V40 shadow causal49/VOL30 evaluation with 0 valid candidates.
- Old paper trader srv-d9v1r42d0e5s73eemsv0 is suspended.
- Old shadow srv-dbf5798m7kps73fqcuog still observes an account position with NATIVE_PROTECTION_LOST and active stop semantic mismatch. Exact old account identity is not proven against the currently flat T100 WEB pilot. DO NOT retire that service until the position and protection are reconciled.
- The shipped V40 live coordinator refuses POST via app/t100_live_guards.py LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED. Do not flip that constant or treat prior BTC isolated test trades as V40 short CROSS 1x SL75 certification.

## Cost-minimal next preflight
New app/t100_v40_render_pilot_readonly_preflight.py --once (offline tested on branch) reuses the established 10 signed GET two-bookend account/economics probe with no MEXC POST, DB, local journal or Discord operation. It does not use the Mac Keychain. Credentials MUST be provisioned through an isolated Render private environment by the operator, never in chat, GitHub, logs, pasted shell commands, the scanner's existing DATABASE_URL principal, or BOXWAVE.

Required private isolated environment variables:

- T100_V40_RENDER_PILOT_PREFLIGHT_ENABLED=true
- T100_V40_PILOT_ROLE=ISOLATED_READONLY_PREFLIGHT
- T100_EXECUTION_MODE=paper
- T100_LIVE_ORDER_API_ENABLED=false
- T100_LIVE_KILL_SWITCH=true
- EXECUTION_ENABLED=false
- TRADING_MODE=paper
- T100_V40_PILOT_READONLY_API_KEY = new dedicated T100 WEB pilot read-only key
- T100_V40_PILOT_READONLY_API_SECRET = its dedicated secret
- T100_V40_PILOT_READONLY_KEY_SHA256 = exact local/offline SHA256 of that key

Verify independently in MEXC UI that the read-only key belongs to the correct T100 WEB pilot subaccount, has NO trade/withdraw rights and the wallet/positions are flat. A key-hash match is not independent identity or permission proof.

Suggested low-cost execution: reuse the old suspended paper-trader service *after* disabling its auto-deploy, replacing its environment with minimum isolated settings (remove DATABASE_URL, old secrets and webhook), and building the reviewed integration branch. Run the program as a **Render One-Off Job**, not a continuously running worker start command (which may auto-restart an exiting program). Render One-Off Jobs use the base service build and env and are billed only while running: https://render.com/docs/one-off-jobs . The currently connected Render integration does not expose job creation. DO NOT resume the retired paper trader as its old runner.

Result on success is V40_RENDER_PILOT_FLAT_ECONOMICS_READ_ONLY_NONAUTHORIZING with the research-only notional ceiling min(USDT 12.50, 25 percent of current verified available/equity). This is NOT an executable lot, loss cap, certification, or permit.

## Release gates prior to ANY first V40 real order
1. Fresh eligible Stage2 with exact 49 causal prior Min30 candles and VOL30 below threshold; not a random BTC proof order.
2. Correct dedicated account identity, server-side permissions, external position and stop reconciliation, no foreign positions/orders.
3. Contract trading access, lot size/step, liquidity, spread, VWAP, fees, SHORT CROSS 1x leverage, total USDT collateral and private risk tier verified on fresh GETs.
4. Strict one-shot native entry+SL75 adapter with irreversible durable pre-POST claim, no retry after ambiguous acknowledgment, exact fills, whole-position native-market stop readback, restart and emergency-close fallback, independent nonflat liveness/alarm channel. Do not weaken the general live adapter safety gate.
5. Final operator-visible approval of exact symbol, quantity, stop price, worst-case scenarios, ability to supervise exchange UI; only one small real trade before widening.

**Remaining service changes and real MEXC POSTs: NONE performed.**
