# V40 guardian archived latch audit — READ ONLY, 2026-11-13

## Current incident and limitation

The guardian scope 003 was created at 17:05:53 UTC, then reported
`V40_GUARDIAN_HEDGE_MODE_SIGNED_GET_FAILED_HALT` at 17:42:05,
19:09:01 and 21:58:40 UTC. Each incident latched and delivered a critical
alert; recent later Render heartbeats report a doubly verified flat account,
but `critical_latched=true` remains.

The old outbox code `OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID`
is a **generic critical category**, not proof the cause was merely an API
GET failure; it can represent more serious provenance/stop issues. Its exact
underlying reason is visible only in bounded observer logs, not archived
incident category alone.

The connected external Render PostgreSQL query failed due to the connector's
TLS/network setup, so exact immutable per-scope database contents were not
independently inspected in chat.

## Proposed read-only audit

`app/t100_v40_guardian_incident_archive_audit.py` reads the four known scope
states and outbox counts in a single REPEATABLE READ READ ONLY transaction
through the guardian's already connected alert writer session.

It emits only fixed scope names, existence, currently-flat observation,
freshness, sticky latch status, revision, and counts of generic/other critical
classes and undelivered alerts. No raw SQL values, account IDs, outbox bodies,
credentials, or scope hashes are emitted.

Hook is disabled by default. To request one startup report in the guardian's
own trusted Render environment, the explicit gate
`T100_V40_GUARDIAN_READONLY_ARCHIVE_AUDIT_ENABLED=true` can be set.

**No incident is reset, no new recovery scope is created, no trade is
authorized, and no exchange requests or DB writes are made by this audit.**
A missing scope, unexpected critical class, or undelivered event is reported
as blocking evidence, not silently corrected. Even a complete all-green
archive count cannot prove actual underlying GET cause or liquidation safety.

The existing project policy prohibits automatically creating a fifth recovery
scope. Any separately reviewed recovery would require independent evidence
of root cause, stable venue GETs and account/ledger flatness, archived incident
delivery, absence of unrelated/unknown positions, and a new explicit
authorization decision. Merely switching a flag must never rearm trading.
