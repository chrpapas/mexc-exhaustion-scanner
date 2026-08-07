-- v0.5.1: safely migrate legacy signal levels to the v0.5 market-state model.
--
-- Legacy v0.4 could store both `watch` and `candidate` for the same
-- (symbol, signaled_at). Both map to `run_watch` in v0.5, so we must
-- deduplicate the prospective target key BEFORE changing the level.

ALTER TABLE run_signals
    DROP CONSTRAINT IF EXISTS run_signals_level_check;

WITH ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY
                symbol,
                signaled_at,
                CASE
                    WHEN level IN ('watch', 'candidate') THEN 'run_watch'
                    ELSE level
                END
            ORDER BY
                score DESC,
                CASE
                    WHEN level = 'candidate' THEN 0
                    WHEN level = 'watch' THEN 1
                    ELSE 2
                END,
                id DESC
        ) AS rn
    FROM run_signals
)
DELETE FROM run_signals AS rs
USING ranked AS r
WHERE rs.id = r.id
  AND r.rn > 1;

UPDATE run_signals
SET level = 'run_watch'
WHERE level IN ('watch', 'candidate');

ALTER TABLE run_signals
    ADD CONSTRAINT run_signals_level_check
    CHECK (level IN ('run_watch', 'exhaustion_watch', 'short_setup'));
