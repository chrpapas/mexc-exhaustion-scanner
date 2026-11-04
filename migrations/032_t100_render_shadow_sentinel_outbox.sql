-- Durable T100 shadow sentinel alert journal; operator-reviewed, CREATE-ONLY.
-- Never applied automatically by Render workers. Run after reviewed backup.
-- Non-idempotent: refuse any preexisting object instead of replacing.
-- No exchange data, prices, keys, account IDs, or live trade permissions.
CREATE TABLE public.t100_shadow_sentinel_state (
    scope_sha256 char(64) PRIMARY KEY
        CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    revision bigint NOT NULL CHECK (revision > 0),
    last_status varchar(96) NOT NULL
        CHECK (last_status ~ '^SHADOW_WITNESS_[A-Z0-9_]{1,64}$'),
    last_alert boolean NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE public.t100_shadow_sentinel_outbox (
    seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope_sha256 char(64) NOT NULL
        REFERENCES public.t100_shadow_sentinel_state(scope_sha256),
    code varchar(96) NOT NULL
        CHECK (code ~ '^SHADOW_WITNESS_[A-Z0-9_]{1,64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_attempt_at timestamptz,
    delivered_at timestamptz
);
CREATE INDEX t100_shadow_sentinel_outbox_pending_idx
ON public.t100_shadow_sentinel_outbox(scope_sha256, seq)
WHERE delivered_at IS NULL;

-- The state UPDATE and alarm INSERT are in the SAME PostgreSQL transaction.
-- Failed worker, crash, or redeploy cannot erase unacknowledged alarms.
-- A new event is created on entering alarm state or changing alarm code,
-- not on every repeated polling cycle. Returning healthy resets only the
-- current state, never deletes historical outbox entries.
CREATE FUNCTION public.t100_shadow_sentinel_append_alarm()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.last_alert THEN
            INSERT INTO public.t100_shadow_sentinel_outbox(scope_sha256, code)
            VALUES (NEW.scope_sha256, NEW.last_status);
        END IF;
    ELSE
        IF NEW.revision <= OLD.revision THEN
            RAISE EXCEPTION 'T100_SENTINEL_NONMONOTONIC_REVISION';
        END IF;
        IF NEW.scope_sha256 IS DISTINCT FROM OLD.scope_sha256 THEN
            RAISE EXCEPTION 'T100_SENTINEL_SCOPE_IMMUTABLE';
        END IF;
        IF NEW.observed_at < OLD.observed_at THEN
            RAISE EXCEPTION 'T100_SENTINEL_CLOCK_REGRESSION';
        END IF;
        IF NEW.last_alert AND
           (NOT OLD.last_alert OR NEW.last_status IS DISTINCT FROM OLD.last_status) THEN
            INSERT INTO public.t100_shadow_sentinel_outbox(scope_sha256, code)
            VALUES (NEW.scope_sha256, NEW.last_status);
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER t100_shadow_sentinel_append_alarm
AFTER INSERT OR UPDATE ON public.t100_shadow_sentinel_state
FOR EACH ROW EXECUTE FUNCTION public.t100_shadow_sentinel_append_alarm();

-- No DELETE/TRUNCATE/CREATE grants to the sentinel runtime.
-- The role will need SELECT on the witness, SELECT/INSERT/UPDATE on
-- sentinel_state and sentinel_outbox, and USAGE on outbox identity sequence.
-- The sentinel does NOT need any MEXC credentials, trade-table privileges
-- or privileges on migrations/027-030. Audit grants before deployment.
