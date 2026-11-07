-- T100 guardian incident channel (REVIEW ONLY / NOT APPLIED).
-- Independent of the existing T100 shadow sentinel and the T100-owned
-- position checkpoints. No MEXC credentials, trading grants or operator
-- authorizations are created. Provision a separate restricted DB role ONLY
-- after release review; no migration is run by an observer worker.
CREATE TABLE public.t100_guardian_incident_state (
    scope_sha256 char(64) PRIMARY KEY
      CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    last_status text NOT NULL
      CHECK (last_status ~ '^OBSERVER_[A-Z0-9_]{1,80}$'),
    last_alert boolean NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    critical_latched boolean NOT NULL DEFAULT false,
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.t100_guardian_incident_outbox (
    seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope_sha256 char(64) NOT NULL
      REFERENCES public.t100_guardian_incident_state(scope_sha256),
    code text NOT NULL
      CHECK (code ~ '^OBSERVER_[A-Z0-9_]{1,80}$'),
    severity text NOT NULL CHECK (severity IN ('warning','critical')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_attempt_at timestamptz,
    delivered_at timestamptz
);
CREATE INDEX t100_guardian_incident_outbox_pending_idx
 ON public.t100_guardian_incident_outbox(scope_sha256,seq)
 WHERE delivered_at IS NULL;

CREATE FUNCTION public.t100_guardian_incident_emit()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.last_alert THEN
            INSERT INTO public.t100_guardian_incident_outbox
                (scope_sha256,code,severity)
            VALUES (NEW.scope_sha256,NEW.last_status,'critical');
        END IF;
    ELSIF NEW.last_status IS DISTINCT FROM OLD.last_status OR
          (NEW.last_alert AND NOT OLD.last_alert) THEN
        INSERT INTO public.t100_guardian_incident_outbox
            (scope_sha256,code,severity)
        VALUES (
            NEW.scope_sha256,NEW.last_status,
            CASE WHEN NEW.last_alert THEN 'critical' ELSE 'warning' END
        );
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER t100_guardian_incident_emit_trigger
AFTER INSERT OR UPDATE ON public.t100_guardian_incident_state
FOR EACH ROW EXECUTE FUNCTION public.t100_guardian_incident_emit();

CREATE FUNCTION public.t100_guardian_incident_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'GUARDIAN_INCIDENT_HISTORY_NO_DELETE';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.scope_sha256 IS DISTINCT FROM OLD.scope_sha256 OR
           NEW.revision <> OLD.revision + 1 OR
           (OLD.critical_latched AND NOT NEW.critical_latched) THEN
            RAISE EXCEPTION 'GUARDIAN_INCIDENT_IDENTITY_OR_LATCH_RESET';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER t100_guardian_incident_guard_trigger
BEFORE UPDATE OR DELETE ON public.t100_guardian_incident_state
FOR EACH ROW EXECUTE FUNCTION public.t100_guardian_incident_guard();

CREATE FUNCTION public.t100_guardian_outbox_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'GUARDIAN_OUTBOX_NO_DELETE';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF (
            NEW.seq, NEW.scope_sha256, NEW.code, NEW.severity,
            NEW.created_at
        ) IS DISTINCT FROM (
            OLD.seq, OLD.scope_sha256, OLD.code, OLD.severity,
            OLD.created_at
        ) OR NEW.attempts < OLD.attempts OR
        (OLD.delivered_at IS NOT NULL AND
         NEW.delivered_at IS DISTINCT FROM OLD.delivered_at) THEN
            RAISE EXCEPTION 'GUARDIAN_OUTBOX_IMMUTABLE';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER t100_guardian_outbox_guard_trigger
BEFORE UPDATE OR DELETE ON public.t100_guardian_incident_outbox
FOR EACH ROW EXECUTE FUNCTION public.t100_guardian_outbox_guard();

-- NO GRANT. Database owner must separately create a role with
-- ONLY SELECT/INSERT/UPDATE on the two guardian incident tables and
-- USAGE on the owned identity sequence. No checkpoint or trade rights.
-- The read-only exchange observer uses a DIFFERENT SELECT-only role.
