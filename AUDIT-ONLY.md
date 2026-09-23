# v1.3.82 Snapshot Audit Build

This build is based on `mexc-exhaustion-scanner-v1.3.82-atr-hard-fix(1).zip` and is intended only to observe the candle snapshots consumed by the existing scanner.

## Safety contract

- `SNAPSHOT_AUDIT_ENABLED` defaults to `false`.
- With auditing disabled, `collect_candles()` follows the original v1.3.82 coroutine/gather path and `_sync_interval_from()` follows the original fetch/upsert path.
- No trader, execution, strategy, scoring, episode, risk, notifier, persistence, scheduler cadence, or retest decision logic was changed.
- No new database table or migration was added.
- Audit output goes only to the application log.
- During an audited candle cycle, per-symbol timings are accumulated in memory and emitted after the cycle completes, in chunks of 25 rows.
- During signal evaluation, audit rows are emitted after the evaluation cycle completes. By default only `breakdown_watch` episodes are included, plus any symbols explicitly requested.

## Environment variables

Keep these unset/false for behavior-identical deployment:

```text
SNAPSHOT_AUDIT_ENABLED=false
```

Enable the observer:

```text
SNAPSHOT_AUDIT_ENABLED=true
SNAPSHOT_AUDIT_STATES=breakdown_watch
SNAPSHOT_AUDIT_SYMBOLS=
```

`SNAPSHOT_AUDIT_SYMBOLS` is a comma-separated optional override, for example:

```text
SNAPSHOT_AUDIT_SYMBOLS=APT_USDT,LIGHT_USDT
```

`*` audits every evaluated symbol, but this produces substantially more log data and is not recommended for routine operation:

```text
SNAPSHOT_AUDIT_SYMBOLS=*
```

## What is recorded

### Candle cycle

For every discovery symbol's Min15 refresh:

- discovery queue index and queue size
- fetch start and finish timestamps
- database write completion timestamp
- prior latest candle timestamp
- exact latest Min15 payload returned by MEXC: open time, OHLC, volume, amount
- number of Min15 rows fetched

This records the actual collector phase and per-symbol refresh timing without changing the fetch/upsert result.

### Signal cycle

For selected symbols (default: active `breakdown_watch` episodes):

- signal evaluation queue index
- exact read timestamp
- episode id/state
- newest Min15 row present in the database
- newest Min15 row considered completed by production
- ticker last price
- risk tier, run score, exhaustion score, market state and scorable flag

For a failed-retest evaluation it additionally records:

- production breakdown timestamp, broken level and ATR
- confirmed / invalidated / expired result
- retest timestamp, high, close and reason

Every structured line starts with:

```text
SNAPSHOT_AUDIT {json}
```

## Extracting downloaded Render logs

```bash
python3 scripts/extract_snapshot_audit.py render.log \
  --jsonl snapshot-audit.jsonl \
  --csv snapshot-audit.csv
```

## Recommended deployment sequence

1. Preserve the current v1.3.82 deployment/zip as rollback.
2. Deploy this code with `SNAPSHOT_AUDIT_ENABLED=false` first.
3. Verify normal scanner/trader operation and standard signal/performance logs.
4. Set only `SNAPSHOT_AUDIT_ENABLED=true` and keep the default state filter (`breakdown_watch`).
5. Collect a fresh forward-validation period before changing any scanner/trader behavior.
6. Export the Render logs and reconstruct point-in-time Min15 snapshots offline from the audit records and Min1 history.

## Tests

The audit-specific config tests are:

```bash
pytest -q tests/test_snapshot_audit_config_v1382.py
```

Run the complete project suite in the normal project environment (where `asyncpg` and the declared dependencies are installed):

```bash
pytest -q
```
