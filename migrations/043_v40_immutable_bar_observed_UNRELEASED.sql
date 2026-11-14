-- UNRELEASED: apply only under separate production migration review.
-- Adds an immutable pre-arm BAR_OBSERVED revision for a strictly improving
-- favorable completed 30m candle while the native MARKET SL is unchanged.
-- Required so durable checkpoint and bar cursor never disagree after restart.
-- The STOP_VERIFIED reason still requires an actual tightened exchange stop.
-- No account/venue change, no new privileges or automatic deploy.

BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='15s';

ALTER TABLE public.t100_live_position_checkpoints
  DROP CONSTRAINT IF EXISTS t100_live_position_checkpoints_last_write_reason_check;
ALTER TABLE public.t100_live_position_checkpoints
  ADD CONSTRAINT t100_live_position_checkpoints_last_write_reason_check
  CHECK (last_write_reason IN (
    'ENTRY_PROTECTED','BAR_OBSERVED','STOP_VERIFIED',
    'REVIEW_LATCHED','CLOSING_LATCHED','CLOSE_VERIFIED'
  ));

ALTER TABLE public.t100_live_position_checkpoint_revisions
  DROP CONSTRAINT IF EXISTS t100_live_position_checkpoint_revisions_write_reason_check;
ALTER TABLE public.t100_live_position_checkpoint_revisions
  ADD CONSTRAINT t100_live_position_checkpoint_revisions_write_reason_check
  CHECK (write_reason IN (
    'ENTRY_PROTECTED','BAR_OBSERVED','STOP_VERIFIED',
    'REVIEW_LATCHED','CLOSING_LATCHED','CLOSE_VERIFIED'
  ));

COMMIT;
