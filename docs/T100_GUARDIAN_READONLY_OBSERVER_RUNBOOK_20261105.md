# T100/225 account-bound read-only guardian observer — deployment runbook

**Engineering-only / development branch / not deployed.**
This is a *GET + SELECT* observer. It cannot open, close, cancel, or
modify an MEXC order; it cannot advance an order journal or provision a
Render worker. It is not the missing production live trader.

## Scope and isolation

- The observer only considers **one** durable T100 checkpoint when
  `position_id` exactly matches a separately pinned configuration.
- The account is pinned to **SHA256 of the MEXC API key**. Never paste or
  save the raw API key or secret in GitHub, issue comments, logs or chat.
- Manual MEXC positions are visible only as foreign exposure; never
  adopted, automatically closed, or modified.
- BOXWAVE is a different subaccount/system and must not be connected.
- The existing `mexc-t100-live-shadow` Render service remains untouched,
  retains its separate branch and historical safety latch.
- Multi-position T100 mode is **not supported** by this one-position
  guardian observer. Multiple nonclosed T100 checkpoints abort.

## Implemented modules

1. `app/t100_live_guardian_checkpoint_reader.py`: pins key digest and
   position, demands a SELECT-only database role inside a READ ONLY
   transaction, reads the original position checkpoint, **entire**
   revision chain, matching ACKed entry intent, and resolved native-stop
   amendments. Unknown revisions, unresolved intents, absent checkpoint
   ownership or ambiguous multi-position state fail closed.
2. `app/t100_live_guardian_readonly_once.py`: opt-in command that obtains
   the checkpoint from PostgreSQL through a hostname-verified TLS
   connection and then uses a **GET-only allowlisted MEXC client** with
   sorted query parameters in the API HMAC signature.
   No order POST method is present. No process-level WebSocket trading
   client, Discord notifier, database migration or persistent write.
3. `app/t100_live_guardian_observer.py`: validates T100 entry origin,
   current native market-stop coverage, and potential trigger races.
   It emits only redacted status booleans/counts. A missing stop never
   directly authorizes a close.
4. `app/t100_live_owned_position_finality.py`: exact closed-position
   history + exact positionId GET verification. **Requires actual
   metadata-backed pagination**. The official MEXC docs' sample sometimes
   shows a bare data list; if the live API uses that shape, this
   finality proof deliberately fails closed until separately corroborated.
5. `tests/test_t100_guardian_reader_postgres_integration.py`: on an
   isolated PostgreSQL 17 CI instance, validates a strictly read-only
   role, denies a mutation-capable role even in READ ONLY transaction,
   rejects other position IDs and missing ownership. Does not connect to
   the user's database.

## One-cycle local self-check (no credentials or network)

```bash
python -m app.t100_live_guardian_readonly_once
```

This prints `OBSERVER_MODULE_INSTALLED_NOT_CONNECTED`; it does NOT
verify any live account and it does not start an ongoing worker.

## Operational prerequisites before real `--once`

**Do not set these secrets in a shared Render service or paste them into
chat.** They should be configured privately in a dedicated observer.

- `T100_GUARDIAN_OBSERVER_ENABLED=true`
- `T100_EXECUTION_MODE=paper`
- `T100_LIVE_ORDER_API_ENABLED=false`
- `T100_LIVE_KILL_SWITCH=true`
- `T100_GUARDIAN_MEXC_API_KEY`: dedicated read-only MEXC key for the
  **specific** subaccount (View Order Details; do not grant trading)
- `T100_GUARDIAN_MEXC_API_SECRET`: matching secret, private
- `T100_GUARDIAN_ACCOUNT_KEY_SHA256`: independently pinned local SHA256
  of that API key, not an arbitrary historical DB scope hash
- `T100_GUARDIAN_POSITION_ID`: exact enrolled T100-owned position ID
- `T100_GUARDIAN_READONLY_DB_DSN`: private restricted **SELECT-only**
  PostgreSQL role whose schema permissions exclude trade and mutation
  operations; no production owner/superuser credentials
- `T100_GUARDIAN_DB_CA_FILE`: trusted CA bundle for TLS hostname
  verification; no insecure TLS fallback

Once these conditions and an actual checkpoint are present, an operator
can invoke:

```bash
python -m app.t100_live_guardian_readonly_once --once
```

The `--once` command is not meant to be called with currently unreviewed
real credentials. It does not run autonomously, create new checkpoints,
repair missing protection, or change the exchange.

## Remaining deployment gates

- **No actual account-scoped read-only role or MEXC view-only key has
  been provisioned** for this observer in the user account.
- No real MEXC history pagination response has been independently
  observed through this new code. A documented response-shape ambiguity
  remains an explicit blocker for historical finality.
- A separate Render observer service has **not** been created.
  Its connection to genuine durable T100 production checkpoints,
  appropriate account, and failure/alerting workflow remains to be
  reviewed. Never point this new observer at the manual account's
  unprotected position just because an open position exists.
- Migration 033 remains review-only, not applied to shared Render DB.
- The original production live **entry executor is still paper-only**.
- No trading authorization follows from any green CI result.

**Acceptance for the next stage:** signed GET-only real account schema
probe, audited restricted DB role and pinned account, one safe observer
cycle with redacted output, then separate disabled-entry Render deploy
and restart/failure alert tests. This is not four-slot or CROSS risk
certification.

## Additional hardening: durable account enrollment and live response shape

**Critical safety fix:** SHA256(key) matching an environment variable does
not establish that the database checkpoint came from that MEXC account.
The guardian now requires a *separate, immutable* database enrollment row
binding all of the following to that exact account: `scope_sha256`,
`position_id`, `symbol`, `external_oid`, `exchange_order_id`.
This binding is checked before any exchange GET.

Review-only `migrations/034_t100_guardian_account_enrollments.sql` creates
the enrollment table and immutability trigger. **It contains no rows,
permissions, credentials or grant to an observer role. It was not applied to
Render.** Enrollment requires independent operator verification against
the dedicated account, then a separate privileged, controlled database
action. Never enroll a manual position or infer ownership from a symbol.

PostgreSQL 17 CI now validates both an actual immutable table trigger and
a SELECT-only observer role. A role with write privilege to this table or
another T100 mutation table is refused even inside a READ ONLY transaction.
If no enrollment exists, the observer must halt on a nonempty checkpoint.

**Official endpoint correction:** MEXC announced a January 2026 Futures API
domain transition to `https://api.mexc.com` and discontinuation of
`https://contract.mexc.com`. The observer now pins the new host. It
never redirects a credentialed request to an unknown domain.

**Real venue response research:** MEXC's official `history_positions`
documentation lists paginated metadata (`totalCount`, `totalPage`,
`currentPage`, `resultList`) while its example has `data` as a bare
array. We must inspect the actual response on the dedicated pilot account
before relying on the historical finality certificate. The metadata
requirement is retained; it is unsafe to manufacture completeness for an
array without total-count evidence.

The code includes a **standard-library, one-request, local Mac Keychain
shape probe**:

```bash
python3 tools/t100_mexc_history_shape_probe_local.py --diagnose
```

Run this only on the user's Mac with the separately held dedicated T100
pilot subaccount credentials in the existing macOS Keychain service
`t100225-mexc-pilot-subaccount`. It performs exactly one signed GET of
`BTC_USDT` history page 1 through the new MEXC API host and prints
only schema types, keys-present flags, and page count. It never prints
position IDs, prices, timestamps, account details, key or secret. It
does not check whether all history was returned, trigger a mutation,
authorize trading, or touch manual positions.

**Result on 2026-11-06:** user ran this exact GET-only probe against the
dedicated T100 pilot account. The live endpoint returned
`ARRAY_WITHOUT_PAGINATION_METADATA`, with 4 rows on page 1 and all
critical positional fields present. None of `pageSize`, `totalCount`,
`totalPage`, `currentPage` appeared. **This result is ground truth
for the observed API shape, but it does not certify full history or
prove that V5's specific position is among those four rows.**
The observer stays development-only and no Render deployment is authorized.

Official MEXC notices:
- https://www.mexc.com/announcements/article/futures-api-access-domain-update-17827791532974
- https://www.mexc.co/api-docs/futures/account-and-trading-endpoints/get-historical-positions

## 2026-11-06 Plan B resolution: positive matching history without fake totals

The user-confirmed real API result is now supported by
`app/t100_live_readonly_probe.py` and
`app/t100_live_owned_position_finality.py`:

- A **bare list** is an incomplete candidate *history page*, never an
  account-wide historical-position inventory or negative proof of absence.
- If that list contains the **exact enrolled T100 position ID**, and two
  successive historical GET reads show the same fully closed SHORT /
  isolated / full contract volume record, with two exact-position GETs
  returning no active position, the **single T100 position** may be
  classified as closed. All other account/manual positions remain
  entirely outside that certification.
- The resulting certificate explicitly sets
  `historical_pages_exhausted=False`, `account_flat_certified=False`,
  `live_authorization=False`, `emergency_close_authorized=False`.
- If the exact ID is absent from a bare history array, the result is
  `NOT_PROVEN`, not `position never existed` or `position closed`.
- Metadata-bearing objects retain their stricter full-page validation.
  History response-shape drift, duplicate position IDs, wrong symbol,
  partial volume, nonterminal state and inconsistent repeated GETs
  stop certification.

**Next read-only V5 verification:** The new standalone local Mac script
`tools/t100_mexc_v5_closed_position_four_get_local.py` reads the existing
V5 durable journal (without changing it), obtains the dedicated pilot
subaccount key and secret from the existing macOS Keychain, sends two
first-page history GETs and two exact position-ID open-position GETs,
and prints only redacted booleans/enums. It NEVER posts an order.
Run only on the user's own Mac with the established journal:

```bash
python3 "$HOME/Downloads/t100_mexc_v5_closed_position_four_get_local.py" --diagnose
```

The user should fetch the reviewed committed script from the repository
into Downloads before running. It does not re-run V5 or authorize entries.

**Original venue ambiguity remains:** A confirmed exact position close does
not establish that MEXC's `triggerSide=1` (documented take-profit) was a
correct label for the loss-direction stop-only V5 close. The stop-loss
trigger-side evidence is still not certified. No Render role migration,
no emergency-close POST deployment, and no four-slot live trading follow.

## 2026-11-06 — Separate worker integration (review only)

A continuous observation-only runtime has been committed in
`app/t100_live_guardian_readonly_worker.py`. This is **not** the
existing Render shadow service and is **not deployed**.

### Runtime behavior

- Start with `python -m app.t100_live_guardian_readonly_worker` (no
  arguments) for a zero-network wiring check. It reports
  `GUARDIAN_WORKER_NOT_STARTED`.
- The explicit `--run` flag executes a repeating signed-GET +
  SELECT-only observation loop, **only if** both
  `T100_GUARDIAN_WORKER_ENABLED=true` and
  `T100_GUARDIAN_OBSERVER_ENABLED=true` are configured with the existing
  strict paper-mode, live-API-disabled, kill-switch-on conditions,
  complete private credentials, exact account and position pins, a
  restricted PostgreSQL role, and a trusted CA.
- No live execution gateway, exchange POST method, stop writer, durable
  DB mutation, migration, account adoption or latch reset is imported.
  The worker produces only redacted JSON status snapshots with boolean
  `foreign_positions_seen` (no exchange IDs/quantities/secrets).
- `OWNED_NATIVE_MARKET_STOP_CURRENTLY_OBSERVED` is classified as
  `OBSERVER_CYCLE_OK`; **this does not mean live trading is safe**.
  A historically closed T100-owned position is classified
  `OBSERVER_CYCLE_INACTIVE` and also does not permit new entries.
- **No enrolled checkpoint is never green**: it reports
  `OBSERVER_UNENROLLED` and alerts. Unknown/stale/missing stop,
  mismatched identity and failed GET/DB cycles alert. Consecutive
  uncertified observations are bounded, default 3, and make the
  process fail closed with nonzero exit. A process restart cannot
  place an order.
- This version is an **observer that emits Render logs**, not a certified
  independent emergency-action guardian. It does not yet deliver
  durable/acknowledged Discord incidents, and its failure counter is
  per-process, not an exchange- or DB-persisted risk latch.

### Safe deployment blueprint

`deploy/t100_guardian_observer.render.REVIEW_ONLY.yaml` is a
**non-root, review-only** Render worker Blueprint. It uses the
development branch, `autoDeployTrigger: off`, distinct name
`mexc-t100-guardian-observer-review-only`, no database
`fromDatabase` reference to an existing account, no embedded
credentials, and the following explicitly disabled defaults:

- `T100_GUARDIAN_WORKER_ENABLED=false`
- `T100_GUARDIAN_OBSERVER_ENABLED=false`
- `T100_EXECUTION_MODE=paper`
- `T100_LIVE_ORDER_API_ENABLED=false`
- `T100_LIVE_KILL_SWITCH=true`

A Blueprint sync can create or modify Render services and incur costs.
**It was not synchronized, and no new Render worker was created.**

### Before any actual deployment

1. Review the separate observer runtime and its Blueprint; do not
   point it at the original manual-position account's credentials or
   reuse the existing Render shadow service.
2. Independently audit and apply account enrollment migration 034
   (not yet applied), verify the dedicated MEXC account, durable
   exact position/entry ID, and SELECT-only PostgreSQL login role.
   Do not auto-enroll positions from a symbol or manual inventory.
3. Provision a **separate view-only Futures MEXC API key** and
   appropriate TLS CA. Mac Keychain credentials are not automatically
   available on Render and must never be copied to GitHub or chat.
4. Test one real GET+SELECT observation with flags still disabled for
   trading. Review redacted logs. Add durable incident notifications and
   restart/failure observation before relying on the worker.
5. Keep the original live trader paper-only until the real entry
   adapter, protective-stop execution verification, account-wide
   risk/collateral certification and exchange post-recovery gates pass.

**Progress classification:** this is audited worker scaffolding with
offline mocks and CI checks, not a running production observer and
definitely not authorization to switch to live risk.
