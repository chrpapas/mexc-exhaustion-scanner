-- TEMPLATE ONLY. DO NOT RUN UNCHANGED.
-- Replace __T100_LIVE_RUNTIME_ROLE__ with the dedicated restricted runtime
-- PostgreSQL user created for the T100 live service.
--
-- This role is intentionally distinct from the migration/table-owner role.
-- It can mutate only the three durable state-machine tables and can append
-- immutable evidence. It receives no DELETE or TRUNCATE rights.

GRANT USAGE ON SCHEMA public TO __T100_LIVE_RUNTIME_ROLE__;

-- Start fail-closed.
REVOKE ALL PRIVILEGES ON TABLE
    t100_live_order_intents,
    t100_live_position_checkpoints,
    t100_live_position_checkpoint_revisions,
    t100_live_stop_amendments,
    t100_live_order_fill_sets,
    t100_live_order_fills,
    t100_live_position_funding_sets,
    t100_live_funding_events,
    t100_live_final_position_accounting
FROM __T100_LIVE_RUNTIME_ROLE__;

-- Durable mutable state machines: no DELETE/TRUNCATE.
GRANT SELECT, INSERT, UPDATE ON TABLE
    t100_live_order_intents,
    t100_live_position_checkpoints,
    t100_live_stop_amendments
TO __T100_LIVE_RUNTIME_ROLE__;

-- Immutable evidence: append + read only.
GRANT SELECT, INSERT ON TABLE
    t100_live_position_checkpoint_revisions,
    t100_live_order_fill_sets,
    t100_live_order_fills,
    t100_live_position_funding_sets,
    t100_live_funding_events,
    t100_live_final_position_accounting
TO __T100_LIVE_RUNTIME_ROLE__;

-- BIGSERIAL inserts need nextval() plus read access. UPDATE is deliberately
-- withheld so setval() is unavailable to the runtime role.
REVOKE ALL PRIVILEGES ON SEQUENCE
    t100_live_order_intents_id_seq,
    t100_live_position_checkpoint_revisions_revision_id_seq,
    t100_live_stop_amendments_id_seq
FROM __T100_LIVE_RUNTIME_ROLE__;

GRANT USAGE, SELECT ON SEQUENCE
    t100_live_order_intents_id_seq,
    t100_live_position_checkpoint_revisions_revision_id_seq,
    t100_live_stop_amendments_id_seq
TO __T100_LIVE_RUNTIME_ROLE__;

-- Verification is performed by app.t100_live_schema_gate under the runtime
-- credential. A rollout must not proceed unless that gate returns:
-- durable_schema_deployed_verified=true.
