-- DRAFT ONLY: immutable final reconciled position accounting.
CREATE TABLE IF NOT EXISTS t100_live_final_position_accounting (
    position_id TEXT PRIMARY KEY,
    entry_order_id TEXT NOT NULL,
    close_order_id TEXT NOT NULL,
    stop_order_id TEXT NOT NULL,
    opened_contracts NUMERIC NOT NULL CHECK (opened_contracts > 0),
    closed_contracts NUMERIC NOT NULL CHECK (closed_contracts > 0),
    open_notional_usdt NUMERIC NOT NULL CHECK (open_notional_usdt > 0),
    close_notional_usdt NUMERIC NOT NULL CHECK (close_notional_usdt > 0),
    realized_gross_usdt NUMERIC NOT NULL,
    fees_usdt NUMERIC NOT NULL,
    funding_cashflow_usdt NUMERIC NOT NULL,
    realized_net_usdt NUMERIC NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (opened_contracts = closed_contracts),
    UNIQUE(entry_order_id),
    UNIQUE(close_order_id)
);
-- Runtime DB role should have no UPDATE/DELETE privilege.
