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
