# Render deployment — v1.3.82 audit-ready

This repository is the full v1.3.82 project with snapshot audit instrumentation enabled only for the scanner worker.

## Scanner worker

Render Blueprint start command:

```text
python -m app.worker
```

Audit env values are declared on `mexc-exhaustion-scanner` only:

```text
SNAPSHOT_AUDIT_ENABLED=true
SNAPSHOT_AUDIT_STATES=breakdown_watch
SNAPSHOT_AUDIT_SYMBOLS=
```

## Trader worker

Trader start command remains unchanged:

```text
python -m app.trader
```

No snapshot-audit env variables are declared on the trader service.

## Build command

Both services retain:

```text
pip install .
```

## After deploy

In the scanner logs, verify both normal scanner activity and lines beginning with:

```text
SNAPSHOT_AUDIT
```

Expected audit event types are `candle_cycle` and `signal_cycle`.

Do not run the production-faithful backtester on Render. Run it locally on the Mac.
