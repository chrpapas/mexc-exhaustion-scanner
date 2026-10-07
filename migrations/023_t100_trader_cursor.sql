-- v1.4.1: restore scanner -> signal DB -> trader architecture.
ALTER TABLE t100_runtime
    ADD COLUMN IF NOT EXISTS last_trader_eval_at timestamptz;
