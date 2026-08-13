-- v1.1.0: make position maturity an independent trader strategy dimension.
-- Existing v1.0 positions default to the +20% target strategy.

ALTER TABLE trader_positions
    ADD COLUMN IF NOT EXISTS position_maturity text NOT NULL DEFAULT 'profit_20'
    CHECK (position_maturity IN ('profit_20', '1d', '2d', '3d', '7d'));
