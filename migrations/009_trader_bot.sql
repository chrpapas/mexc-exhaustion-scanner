-- v1.0.0: one-position MEXC STANDARD-signal trader, paper first and live-ready.

CREATE TABLE IF NOT EXISTS trader_runtime (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    last_signal_id bigint NOT NULL DEFAULT 0,
    paper_equity_usdt double precision NOT NULL CHECK (paper_equity_usdt >= 0),
    initialized_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trader_positions (
    id bigserial PRIMARY KEY,
    signal_id bigint NOT NULL UNIQUE REFERENCES run_signals(id) ON DELETE RESTRICT,
    episode_id bigint REFERENCES pump_episodes(id) ON DELETE SET NULL,
    symbol text NOT NULL REFERENCES contracts(symbol) ON DELETE RESTRICT,
    mode text NOT NULL CHECK (mode IN ('paper', 'live')),
    capital_strategy text NOT NULL CHECK (capital_strategy IN ('isolated_full', 'cross_20')),
    exit_strategy text NOT NULL CHECK (exit_strategy IN ('ratchet_5', 'trailing_5')),
    status text NOT NULL CHECK (status IN ('open', 'closed', 'liquidated', 'error')),
    opened_at timestamptz NOT NULL,
    closed_at timestamptz,
    entry_price double precision NOT NULL CHECK (entry_price > 0),
    exit_price double precision,
    entry_equity_usdt double precision NOT NULL CHECK (entry_equity_usdt > 0),
    notional_usdt double precision NOT NULL CHECK (notional_usdt > 0),
    quantity_base double precision NOT NULL CHECK (quantity_base > 0),
    current_price double precision NOT NULL CHECK (current_price > 0),
    current_return_pct double precision NOT NULL DEFAULT 0,
    peak_profit_pct double precision NOT NULL DEFAULT 0,
    max_adverse_pct double precision NOT NULL DEFAULT 0,
    profit_floor_pct double precision,
    liquidation_proxy_pct double precision NOT NULL,
    realized_pnl_usdt double precision,
    realized_return_pct double precision,
    exit_reason text,
    mexc_position_id bigint,
    mexc_open_order_id bigint,
    mexc_close_order_id bigint,
    last_observed_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_trader_single_open_position
    ON trader_positions ((1)) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS ix_trader_positions_opened_at
    ON trader_positions (opened_at DESC);

CREATE TABLE IF NOT EXISTS trader_signal_decisions (
    signal_id bigint PRIMARY KEY REFERENCES run_signals(id) ON DELETE CASCADE,
    decision text NOT NULL CHECK (decision IN (
        'accepted', 'ignored_busy', 'ignored_risk', 'ignored_stale', 'ignored_invalid', 'error'
    )),
    position_id bigint REFERENCES trader_positions(id) ON DELETE SET NULL,
    decided_at timestamptz NOT NULL DEFAULT now(),
    reason text
);

CREATE TABLE IF NOT EXISTS trader_position_events (
    id bigserial PRIMARY KEY,
    position_id bigint NOT NULL REFERENCES trader_positions(id) ON DELETE CASCADE,
    event_type text NOT NULL,
    event_at timestamptz NOT NULL DEFAULT now(),
    price double precision,
    return_pct double precision,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_trader_position_events_position_time
    ON trader_position_events (position_id, event_at DESC);
