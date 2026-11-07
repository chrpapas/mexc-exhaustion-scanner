-- T100/225 owned emergency-close once-only intent ledger.
-- REVIEW ONLY. Not applied by runtime, not included in Render startup.
-- This stores exactly one immutable close intent per owned account+position.
-- NO grants here; use a separate narrow role only after audited deployment.
CREATE TABLE public.t100_owned_emergency_close_intents (
    external_oid varchar(29) PRIMARY KEY
      CHECK (external_oid ~ '^t100gc_[a-f0-9]{22}$'),
    scope_sha256 char(64) NOT NULL
      CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    symbol text NOT NULL CHECK (symbol ~ '^[A-Z0-9]+_USDT$'),
    position_id text NOT NULL CHECK (position_id ~ '^[1-9][0-9]*$'),
    entry_external_oid text NOT NULL CHECK (entry_external_oid LIKE 't100%'),
    entry_order_id text NOT NULL CHECK (entry_order_id ~ '^[1-9][0-9]*$'),
    volume_contracts numeric NOT NULL CHECK (volume_contracts > 0),
    checkpoint_version bigint NOT NULL CHECK (checkpoint_version > 0),
    state text NOT NULL DEFAULT 'PREPARED'
      CHECK (state IN ('PREPARED','SUBMITTING','UNKNOWN','VERIFIED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope_sha256, symbol, position_id)
);

CREATE FUNCTION public.t100_owned_emergency_close_no_reset()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'T100_OWNED_EMERGENCY_CLOSE_NO_DELETE';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.external_oid, NEW.scope_sha256, NEW.symbol,
            NEW.position_id, NEW.entry_external_oid, NEW.entry_order_id,
            NEW.volume_contracts, NEW.checkpoint_version, NEW.created_at)
           IS DISTINCT FROM
           (OLD.external_oid, OLD.scope_sha256, OLD.symbol,
            OLD.position_id, OLD.entry_external_oid, OLD.entry_order_id,
            OLD.volume_contracts, OLD.checkpoint_version, OLD.created_at)
        THEN
            RAISE EXCEPTION 'T100_OWNED_CLOSE_INTENT_IDENTITY_IMMUTABLE';
        END IF;
        IF NOT (
            (OLD.state='PREPARED' AND NEW.state='SUBMITTING') OR
            (OLD.state='SUBMITTING' AND NEW.state IN ('UNKNOWN','VERIFIED')) OR
            (OLD.state='UNKNOWN' AND NEW.state='VERIFIED')
        ) THEN
            RAISE EXCEPTION 'T100_OWNED_CLOSE_INTENT_STATE_NO_RESET';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER t100_owned_emergency_close_no_reset_trigger
BEFORE UPDATE OR DELETE ON public.t100_owned_emergency_close_intents
FOR EACH ROW EXECUTE FUNCTION public.t100_owned_emergency_close_no_reset();

-- Intentionally no GRANT, no role, no seed data, no service changes.
