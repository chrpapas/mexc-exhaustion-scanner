from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.db import Database
from app.json_utils import json_object
from app.trader_models import TradeSignal, TraderPosition


class TraderRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def initialize_runtime(self, *, starting_equity: float, process_existing: bool) -> None:
        current = await self.db.pool.fetchrow(
            "SELECT last_signal_id, paper_equity_usdt FROM trader_runtime WHERE singleton=true"
        )
        if current:
            return
        cursor = 0
        if not process_existing:
            cursor = int(
                await self.db.pool.fetchval(
                    "SELECT COALESCE(MAX(id),0) FROM run_signals WHERE level='confirmed_short'"
                )
                or 0
            )
        await self.db.pool.execute(
            """
            INSERT INTO trader_runtime(singleton, last_signal_id, paper_equity_usdt)
            VALUES (true, $1, $2)
            ON CONFLICT (singleton) DO NOTHING
            """,
            cursor,
            starting_equity,
        )

    async def runtime(self) -> dict[str, Any]:
        row = await self.db.pool.fetchrow(
            "SELECT last_signal_id, paper_equity_usdt, initialized_at, updated_at "
            "FROM trader_runtime WHERE singleton=true"
        )
        if not row:
            raise RuntimeError("trader runtime not initialized")
        return dict(row)

    async def set_cursor(self, signal_id: int) -> None:
        await self.db.pool.execute(
            """
            UPDATE trader_runtime
            SET last_signal_id=GREATEST(last_signal_id,$1), updated_at=now()
            WHERE singleton=true
            """,
            signal_id,
        )

    async def set_paper_equity(self, equity: float) -> None:
        await self.db.pool.execute(
            """
            UPDATE trader_runtime SET paper_equity_usdt=$1, updated_at=now()
            WHERE singleton=true
            """,
            equity,
        )

    async def next_confirmed_signals(self, after_id: int, limit: int = 100) -> list[TradeSignal]:
        rows = await self.db.pool.fetch(
            """
            SELECT id, symbol, signaled_at, episode_id, features
            FROM run_signals
            WHERE id > $1 AND level='confirmed_short'
            ORDER BY id ASC
            LIMIT $2
            """,
            after_id,
            limit,
        )
        result: list[TradeSignal] = []
        for row in rows:
            features = json_object(row["features"])
            risk = str(features.get("risk_tier") or "").upper()
            entry_hint = features.get("retest_close")
            try:
                parsed_entry = float(entry_hint) if entry_hint is not None else None
            except (TypeError, ValueError):
                parsed_entry = None
            result.append(
                TradeSignal(
                    id=int(row["id"]),
                    symbol=str(row["symbol"]),
                    signaled_at=row["signaled_at"],
                    episode_id=int(row["episode_id"]) if row["episode_id"] is not None else None,
                    entry_hint=parsed_entry,
                    risk_tier=risk,
                    features=features,
                )
            )
        return result

    async def decision(self, signal_id: int, decision: str, reason: str, position_id: int | None = None) -> None:
        await self.db.pool.execute(
            """
            INSERT INTO trader_signal_decisions(signal_id, decision, position_id, reason)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (signal_id) DO UPDATE SET
                decision=EXCLUDED.decision,
                position_id=EXCLUDED.position_id,
                reason=EXCLUDED.reason,
                decided_at=now()
            """,
            signal_id,
            decision,
            position_id,
            reason,
        )

    async def active_position(self) -> TraderPosition | None:
        row = await self.db.pool.fetchrow(
            "SELECT * FROM trader_positions WHERE status='open' ORDER BY id DESC LIMIT 1"
        )
        return self._position(row) if row else None

    async def create_position(
        self,
        *,
        signal: TradeSignal,
        mode: str,
        capital_strategy: str,
        exit_strategy: str,
        position_maturity: str,
        entry_price: float,
        entry_equity_usdt: float,
        notional_usdt: float,
        quantity_base: float,
        liquidation_proxy_pct: float,
        mexc_position_id: int | None = None,
        mexc_open_order_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraderPosition:
        row = await self.db.pool.fetchrow(
            """
            INSERT INTO trader_positions(
                signal_id, episode_id, symbol, mode, capital_strategy, exit_strategy, position_maturity,
                status, opened_at, entry_price, entry_equity_usdt, notional_usdt,
                quantity_base, current_price, current_return_pct, peak_profit_pct,
                max_adverse_pct, liquidation_proxy_pct, mexc_position_id,
                mexc_open_order_id, metadata
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,'open',$8,$9,$10,$11,$12,$9,0,0,0,$13,$14,$15,$16::jsonb
            )
            RETURNING *
            """,
            signal.id,
            signal.episode_id,
            signal.symbol,
            mode,
            capital_strategy,
            exit_strategy,
            position_maturity,
            datetime.now(UTC),
            entry_price,
            entry_equity_usdt,
            notional_usdt,
            quantity_base,
            liquidation_proxy_pct,
            mexc_position_id,
            mexc_open_order_id,
            json.dumps(metadata or {}, separators=(",", ":"), default=str),
        )
        await self.decision(signal.id, "accepted", "STANDARD signal accepted", int(row["id"]))
        await self.add_event(int(row["id"]), "opened", entry_price, 0.0, metadata or {})
        return self._position(row)

    async def update_market(
        self,
        position_id: int,
        *,
        price: float,
        return_pct: float,
        peak_profit_pct: float,
        max_adverse_pct: float,
        profit_floor_pct: float | None,
        event: str | None = None,
    ) -> TraderPosition:
        row = await self.db.pool.fetchrow(
            """
            UPDATE trader_positions
            SET current_price=$2,
                current_return_pct=$3,
                peak_profit_pct=$4,
                max_adverse_pct=$5,
                profit_floor_pct=$6,
                last_observed_at=now(),
                updated_at=now()
            WHERE id=$1 AND status='open'
            RETURNING *
            """,
            position_id,
            price,
            return_pct,
            peak_profit_pct,
            max_adverse_pct,
            profit_floor_pct,
        )
        if not row:
            raise RuntimeError(f"position {position_id} is no longer open")
        if event:
            await self.add_event(
                position_id,
                event,
                price,
                return_pct,
                {"profit_floor_pct": profit_floor_pct, "peak_profit_pct": peak_profit_pct},
            )
        return self._position(row)

    async def close_position(
        self,
        position: TraderPosition,
        *,
        exit_price: float,
        status: str,
        reason: str,
        mexc_close_order_id: int | None = None,
    ) -> TraderPosition:
        realized_return_pct = (position.entry_price - exit_price) / position.entry_price * 100.0
        realized_pnl = position.quantity_base * (position.entry_price - exit_price)
        row = await self.db.pool.fetchrow(
            """
            UPDATE trader_positions
            SET status=$2, closed_at=now(), exit_price=$3,
                current_price=$3, current_return_pct=$4,
                realized_pnl_usdt=$5, realized_return_pct=$4,
                exit_reason=$6, mexc_close_order_id=$7,
                last_observed_at=now(), updated_at=now()
            WHERE id=$1 AND status='open'
            RETURNING *
            """,
            position.id,
            status,
            exit_price,
            realized_return_pct,
            realized_pnl,
            reason,
            mexc_close_order_id,
        )
        if not row:
            raise RuntimeError(f"position {position.id} is no longer open")
        await self.add_event(
            position.id,
            status,
            exit_price,
            realized_return_pct,
            {"reason": reason, "realized_pnl_usdt": realized_pnl},
        )
        return self._position(row)

    async def add_event(
        self,
        position_id: int,
        event_type: str,
        price: float | None,
        return_pct: float | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self.db.pool.execute(
            """
            INSERT INTO trader_position_events(position_id,event_type,price,return_pct,payload)
            VALUES ($1,$2,$3,$4,$5::jsonb)
            """,
            position_id,
            event_type,
            price,
            return_pct,
            json.dumps(payload or {}, separators=(",", ":"), default=str),
        )

    @staticmethod
    def _position(row: Any) -> TraderPosition:
        metadata = json_object(row["metadata"])
        return TraderPosition(
            id=int(row["id"]),
            signal_id=int(row["signal_id"]),
            symbol=str(row["symbol"]),
            mode=str(row["mode"]),
            capital_strategy=str(row["capital_strategy"]),
            exit_strategy=str(row["exit_strategy"]),
            position_maturity=str(row["position_maturity"]),
            status=str(row["status"]),
            opened_at=row["opened_at"],
            entry_price=float(row["entry_price"]),
            entry_equity_usdt=float(row["entry_equity_usdt"]),
            notional_usdt=float(row["notional_usdt"]),
            quantity_base=float(row["quantity_base"]),
            current_price=float(row["current_price"]),
            current_return_pct=float(row["current_return_pct"]),
            peak_profit_pct=float(row["peak_profit_pct"]),
            max_adverse_pct=float(row["max_adverse_pct"]),
            profit_floor_pct=(
                float(row["profit_floor_pct"]) if row["profit_floor_pct"] is not None else None
            ),
            liquidation_proxy_pct=float(row["liquidation_proxy_pct"]),
            mexc_position_id=(
                int(row["mexc_position_id"]) if row["mexc_position_id"] is not None else None
            ),
            mexc_open_order_id=(
                int(row["mexc_open_order_id"]) if row["mexc_open_order_id"] is not None else None
            ),
            metadata=metadata,
        )
