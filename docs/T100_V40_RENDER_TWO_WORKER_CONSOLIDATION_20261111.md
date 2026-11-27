# T100/225 V40 Render consolidation — reviewed plan, 11 November 2026

**Proposal and implemented branch candidate; NOT YET DEPLOYED.** This
document records the desired *new V40* architecture and current Render
inventory. It does not authorize trade entry, MEXC POSTs, or deletion of
unreconciled positions/monitoring. BOXWAVE is separate and untouched.

## Actual Render inventory (workspace Trading bots, verified 2026-11-11)

| Service | Service ID | Auto deploy | Branch | Start command | Role and disposition |
| --- | --- | --- | --- | --- | --- |
| mexc-exhaustion-scanner | srv-d9kba2jm8hqs73c3dfi0 | yes | main | python -m app.t100_200_scanner | REUSE Stage2 signal producer, then V40 scanner-shadow validator |
| mexc-standard-short-trader | srv-d9v1r42d0e5s73eemsv0 | yes | main | python -m app.t100_200_trader | RETIRE old paper portfolio when confirmed no reporting dependencies; V40 ignores mutable paper trade_decision |
| mexc-t100-live-shadow | srv-dbf5798m7kps73fqcuog | no | feature/t100-live-readiness-gates-20261023 | python -m app.t100_live_shadow_runtime | HOLD: current logs still observe an account position and stop semantic mismatch with native-protection kill latch |

Current scanner uses Render Frankfurt 1c-2g, old paper trader
`starter`, old shadow `0.5c-512mb`. All three are **paid running
workers**. Avoid creating another billable background worker. Existing
Render Postgres is shared and still needed for fresh Stage2 and 49-bar
candle source. Do not delete the DB or its data.

## Target cost footprint

- **One** existing scanner worker, repointed to
  `python -m app.t100_v40_render_stage2_scanner` from integration branch
  after a controlled cutover. It retains the real Stage2/Min30 source
  generation, performs a strict V40 causal 49-bar/VOL30 observation of
  *new* events, and omits old performance-dashboard output. **Paper mode
  only**, no live order transport. It validates existing scanner PostgreSQL
  identity and strategy WITHOUT auto-applying new schema migrations.
- **One** existing paid worker for independent V40 account/stop observation
  and later separately certified execution (prefer repurposing the old
  shadow's smaller plan if its existing account is safely reconciled;
  alternatively reuse the old paper-trader's worker slot). Do not run the
  watchdog and order executor under the privileged scanner-owner database
  credentials. The Mac Keychain read-only key and Discord webhook secrets
  are NOT automatically available on Render. They must be entered into
  isolated Render service env settings by the operator, never pasted in
  chat or committed to GitHub.
- No new paid Redis, web service, worker, storage or cron job while this
  consolidation is being evaluated. A genuinely independent persistent
  incident journal/witness and independent death-alert mechanism are still
  required for *trading* certification; Render's ephemeral local filesystem
  and hourly GitHub CI monitor do not guarantee such durability.
- Keep `T100_EXECUTION_MODE=paper`,
  `T100_LIVE_ORDER_API_ENABLED=false`,
  `T100_LIVE_KILL_SWITCH=true`,
  `EXECUTION_ENABLED=false`,
  `TRADING_MODE=paper` throughout migration.

## Why the old scanner cannot just be killed

Frozen V40 requires **fresh Stage2 events** from the existing scanner's
`public.t100_p2_signals` and exactly 49 prior completed Min30 candles
from `public.candles`. The old `mexc-standard-short-trader` is *not*
required for signal discovery; its paper `trade_decision`, positions,
paper fills and dashboard may be retired independently. The old scanner
currently logged a successful evaluation at
`2026-11-11T10:30:00+00:00` with 82 evaluated symbols and 0 eligible
Stage2. An empty 15-minute signal window is not a scanner error.

## Blocker before retiring old shadow

At `2026-11-11T10:55Z`, the existing Render shadow reported
`position_observed=True`, `tpsl_observed=True`,
`OBSERVED_CURRENT_TPSL_SEMANTIC_MISMATCH`,
`OBSERVING_KILL_LATCHED` with a native-protection kill latch.
The new T100 WEB-pilot **dedicated read-only** Mac GET preflight and
one-cycle watchdog observed FLAT at a different time. **Do not infer
these are the same exchange subaccount** or ignore the possibility
of an open unprotected position. The shadow cannot be stopped safely
until its exchange account, position, native stop and external ownership
have been independently reconciled. Do not adopt, close, cancel,
amend or change its exchange position automatically.

## Safe cutover order

1. **Retire old PAPER trader first to reduce cost**, only after reviewing
   its final paper accounting/report dependencies and confirming paper
   mode. Its present code rejects live trading. Use the Render Dashboard
   to **Suspend** (reversible) the specific service
   `mexc-standard-short-trader` ID `srv-d9v1r42d0e5s73eemsv0`.
   Do not delete the underlying PostgreSQL, repositories, DB schema,
   Stage2 rows or candles. This project cannot currently call a Render
   suspend/retire API action through the connected Render tool, so no
   suspension has been executed.
2. **Prepare scanner shadow cutover in-place.** On scanner service
   `srv-d9kba2jm8hqs73c3dfi0`, disable auto-deploy before switching
   from `main` to the reviewed integration branch, set the entrypoint
   `python -m app.t100_v40_render_stage2_scanner`, and install all
   explicit paper/kill environment flags. The scanner's original
   `DATABASE_URL` remains ONLY on the scanner and is not given to any
   account guardian/executor. Deploy a reviewed revision once, validate
   runtime source health/Stage2 activity and complete a safe rollback
   rehearsal. Do not push a breaking root render.yaml to main.
3. **Reconcile the old shadow's real account first** (potential position,
   stop mismatch, active kill). Keep monitoring while resolving this
   safety incident. Only after verified flat/closed state or positively
   verified replacement protection/monitoring may its paid service be
   suspended or repurposed for the isolated V40 account watch.
4. **Host V40 guardian in an existing worker slot** after review of
   Render env secrets, service principal, and durable incident outbox.
   The current Mac-specific V40 watchdog uses Keychain and local SQLite
   and MUST NOT simply be pointed at Render (no Keychain; ephemeral
   journal, lost latches/restart).
5. **Only then consider live V40 trade execution** after fresh signal
   transport, exact market lot/VWAP/liquidity, CROSS risk/collateral,
   native stop execution/amendment, multi-slot ownership and independent
   nonflat restart/dropout safety are certified. None are certified by
   the scanner-shadow deployment.

## Tool boundary and current status

The connected Render tools provide service listing, logs, deploy
readback, environment updates, new-service creation and deploy triggers;
they **do not expose update-existing-service start-command/branch or
suspend/delete-service operations**. No live Render service was changed
while preparing this PR. Do not claim cost was saved until Render confirms
a worker is suspended/deleted, and do not say the V40 scanner is deployed
until its actual service command/branch/deploy can be verified.

Current design target: 3 paid workers → **2 paid workers** without
creating more. Exact monthly savings depend on the actual active plan and
compute usage in the Render Billing screen. `live_authorization=false`
and `entry_allowed=false` for every step documented here.
