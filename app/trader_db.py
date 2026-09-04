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
            "SELECT last_signal_id, paper_equity_usdt, active_run_id FROM trader_runtime WHERE singleton=true"
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
            "SELECT last_signal_id, paper_equity_usdt, active_run_id, initialized_at, updated_at "
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
            "UPDATE trader_runtime SET paper_equity_usdt=$1, updated_at=now() WHERE singleton=true",
            equity,
        )

    async def latest_confirmed_signal_id(self) -> int:
        return int(
            await self.db.pool.fetchval(
                "SELECT COALESCE(MAX(id),0) FROM run_signals WHERE level='confirmed_short'"
            )
            or 0
        )

    async def run_record(self, run_id: str) -> dict[str, Any] | None:
        row = await self.db.pool.fetchrow(
            "SELECT run_id, mode, strategy_name, status, started_at, ended_at "
            "FROM trader_runs WHERE run_id=$1",
            run_id,
        )
        return dict(row) if row else None

    async def activate_paper_run(
        self, *, run_id: str, starting_equity: float, process_existing: bool, strategy_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        runtime = await self.runtime()
        if str(runtime.get("active_run_id") or "") == run_id:
            return False
        existing = await self.run_record(run_id)
        if existing:
            raise RuntimeError(
                f"TRADER_PAPER_RUN_ID {run_id!r} was already used; choose a new unique run ID"
            )
        cursor = 0 if process_existing else await self.latest_confirmed_signal_id()
        old_run = str(runtime.get("active_run_id") or "legacy_pre_v136")
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE trader_runs SET status='archived', ended_at=COALESCE(ended_at,now()) "
                    "WHERE run_id=$1 AND status='active'",
                    old_run,
                )
                await conn.execute(
                    """
                    INSERT INTO trader_runs(run_id, mode, strategy_name, starting_equity_usdt, status, metadata)
                    VALUES ($1,'paper',$2,$3,'active',$4::jsonb)
                    """,
                    run_id, strategy_name, starting_equity,
                    json.dumps(metadata or {}, separators=(",", ":"), default=str),
                )
                await conn.execute(
                    """
                    UPDATE trader_runtime
                    SET last_signal_id=$1, paper_equity_usdt=$2, active_run_id=$3,
                        initialized_at=now(), updated_at=now()
                    WHERE singleton=true
                    """,
                    cursor, starting_equity, run_id,
                )
        return True

    async def next_confirmed_signals(self, after_id: int, limit: int = 100) -> list[TradeSignal]:
        rows = await self.db.pool.fetch(
            """
            SELECT rs.id, rs.symbol, rs.signaled_at, rs.episode_id, rs.features,
                   pe.started_at AS episode_started_at, pe.breakdown_at AS episode_breakdown_at
            FROM run_signals rs
            LEFT JOIN pump_episodes pe ON pe.id = rs.episode_id
            WHERE rs.id > $1 AND rs.level='confirmed_short'
            ORDER BY rs.id ASC
            LIMIT $2
            """,
            after_id,
            limit,
        )
        result: list[TradeSignal] = []
        for row in rows:
            features = json_object(row["features"])
            if features.get("episode_started_at") is None and row["episode_started_at"] is not None:
                features["episode_started_at"] = row["episode_started_at"].isoformat()
            if features.get("breakdown_at") is None and row["episode_breakdown_at"] is not None:
                features["breakdown_at"] = row["episode_breakdown_at"].isoformat()
            if features.get("hours_run_to_breakdown") is None:
                started_at = row["episode_started_at"]
                breakdown_at = row["episode_breakdown_at"]
                if started_at is not None and breakdown_at is not None:
                    features["hours_run_to_breakdown"] = max(
                        0.0, (breakdown_at - started_at).total_seconds() / 3600.0
                    )
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

    async def active_positions(self) -> list[TraderPosition]:
        rows = await self.db.pool.fetch(
            "SELECT * FROM trader_positions WHERE status='open' ORDER BY slot_no NULLS LAST, opened_at, id"
        )
        return [self._position(r) for r in rows]

    async def active_position(self) -> TraderPosition | None:
        positions = await self.active_positions()
        return positions[0] if positions else None

    async def position(self, position_id: int) -> TraderPosition | None:
        row = await self.db.pool.fetchrow("SELECT * FROM trader_positions WHERE id=$1", position_id)
        return self._position(row) if row else None

    async def portfolio_stats(self, run_id: str | None = None) -> dict[str, Any]:
        row = await self.db.pool.fetchrow(
            """
            SELECT
              COUNT(*) FILTER (WHERE status IN ('closed','liquidated')) AS closed_count,
              COUNT(*) FILTER (WHERE status='liquidated') AS liquidation_count,
              COUNT(*) FILTER (WHERE status='closed' AND COALESCE(realized_return_pct,0) > 0) AS win_count,
              COALESCE(SUM(realized_pnl_usdt) FILTER (WHERE status IN ('closed','liquidated')),0) AS realized_pnl,
              COALESCE(SUM(entry_fee_usdt + exit_fee_usdt),0) AS fees,
              COUNT(*) FILTER (WHERE status='open') AS open_count
            FROM trader_positions
            WHERE ($1::text IS NULL OR run_id=$1)
            """,
            run_id,
        )
        return dict(row) if row else {}

    async def create_position(
        self,
        *,
        signal: TradeSignal,
        run_id: str,
        slot_no: int,
        mode: str,
        capital_strategy: str,
        exit_strategy: str,
        position_maturity: str,
        entry_price: float,
        entry_equity_usdt: float,
        notional_usdt: float,
        quantity_base: float,
        liquidation_proxy_pct: float,
        entry_fee_usdt: float = 0.0,
        mexc_position_id: int | None = None,
        mexc_open_order_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraderPosition:
        row = await self.db.pool.fetchrow(
            """
            INSERT INTO trader_positions(
                signal_id, episode_id, symbol, risk_tier, run_id, slot_no, mode, capital_strategy,
                exit_strategy, position_maturity, status, opened_at, entry_price,
                entry_equity_usdt, notional_usdt, quantity_base, current_price,
                current_return_pct, peak_profit_pct, max_adverse_pct, liquidation_proxy_pct,
                entry_fee_usdt, mexc_position_id, mexc_open_order_id, metadata
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'open',$11,$12,$13,$14,$15,$12,0,0,0,$16,$17,$18,$19,$20::jsonb
            ) RETURNING *
            """,
            signal.id,
            signal.episode_id,
            signal.symbol,
            signal.risk_tier,
            run_id,
            slot_no,
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
            entry_fee_usdt,
            mexc_position_id,
            mexc_open_order_id,
            json.dumps(metadata or {}, separators=(",", ":"), default=str),
        )
        await self.decision(signal.id, "accepted", f"{signal.risk_tier} signal accepted", int(row["id"]))
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
    ) -> TraderPosition:
        row = await self.db.pool.fetchrow(
            """
            UPDATE trader_positions
            SET current_price=$2, current_return_pct=$3, peak_profit_pct=$4,
                max_adverse_pct=$5, profit_floor_pct=$6,
                last_observed_at=now(), updated_at=now()
            WHERE id=$1 AND status='open' RETURNING *
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
        return self._position(row)

    async def mark_target_hit(self, position_id: int, *, price: float, return_pct: float) -> bool:
        row = await self.db.pool.fetchrow(
            """
            UPDATE trader_positions SET target_20_at=COALESCE(target_20_at,now()), updated_at=now()
            WHERE id=$1 AND status='open' AND target_20_at IS NULL RETURNING id
            """,
            position_id,
        )
        if not row:
            return False
        await self.add_event(position_id, "target_20_hit", price, return_pct, {})
        return True

    async def mark_protection_armed(
        self, position_id: int, *, order_id: int | None, floor_pct: float, price: float, return_pct: float
    ) -> bool:
        row = await self.db.pool.fetchrow(
            """
            UPDATE trader_positions
            SET protection_armed_at=COALESCE(protection_armed_at,now()),
                mexc_protection_order_id=COALESCE($2,mexc_protection_order_id),
                profit_floor_pct=GREATEST(COALESCE(profit_floor_pct,-1e9),$3), updated_at=now()
            WHERE id=$1 AND status='open' AND protection_armed_at IS NULL RETURNING id
            """,
            position_id,
            order_id,
            floor_pct,
        )
        if not row:
            return False
        await self.add_event(position_id, "protection_armed", price, return_pct, {"floor_pct": floor_pct, "order_id": order_id})
        return True

    async def set_protection(self, position_id: int, *, order_id: int | None, floor_pct: float) -> None:
        await self.db.pool.execute(
            """
            UPDATE trader_positions
            SET mexc_protection_order_id=COALESCE($2,mexc_protection_order_id),
                profit_floor_pct=GREATEST(COALESCE(profit_floor_pct,-1e9),$3), updated_at=now()
            WHERE id=$1 AND status='open'
            """,
            position_id,
            order_id,
            floor_pct,
        )

    async def mark_breach(self, position_id: int, threshold: int, *, price: float, return_pct: float) -> bool:
        columns = {100: "breach_100_at", 200: "breach_200_at", 300: "breach_300_at", 400: "breach_400_at"}
        column = columns[threshold]
        row = await self.db.pool.fetchrow(
            f"UPDATE trader_positions SET {column}=now(), updated_at=now() "
            f"WHERE id=$1 AND status='open' AND {column} IS NULL RETURNING id",
            position_id,
        )
        if not row:
            return False
        await self.add_event(position_id, f"breach_{threshold}", price, return_pct, {"threshold": threshold})
        return True

    async def patch_metadata(self, position_id: int, patch: dict[str, Any]) -> None:
        await self.db.pool.execute(
            "UPDATE trader_positions SET metadata=metadata || $2::jsonb, updated_at=now() WHERE id=$1",
            position_id,
            json.dumps(patch, separators=(",", ":"), default=str),
        )

    async def close_position(
        self,
        position: TraderPosition,
        *,
        exit_price: float,
        status: str,
        reason: str,
        exit_fee_usdt: float = 0.0,
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
                exit_reason=$6, mexc_close_order_id=$7, exit_fee_usdt=$8,
                last_observed_at=now(), updated_at=now()
            WHERE id=$1 AND status='open' RETURNING *
            """,
            position.id,
            status,
            exit_price,
            realized_return_pct,
            realized_pnl,
            reason,
            mexc_close_order_id,
            exit_fee_usdt,
        )
        if not row:
            raise RuntimeError(f"position {position.id} is no longer open")
        await self.add_event(
            position.id, status, exit_price, realized_return_pct,
            {"reason": reason, "realized_pnl_usdt": realized_pnl, "exit_fee_usdt": exit_fee_usdt},
        )
        return self._position(row)

    async def add_event(
        self, position_id: int, event_type: str, price: float | None, return_pct: float | None,
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
            risk_tier=str(row.get("risk_tier") or metadata.get("risk_tier") or "STANDARD").upper(),
            run_id=str(row.get("run_id") or "legacy_pre_v136"),
            slot_no=int(row["slot_no"]) if row.get("slot_no") is not None else None,
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
            profit_floor_pct=float(row["profit_floor_pct"]) if row["profit_floor_pct"] is not None else None,
            liquidation_proxy_pct=float(row["liquidation_proxy_pct"]),
            target_20_at=row.get("target_20_at"),
            protection_armed_at=row.get("protection_armed_at"),
            mexc_protection_order_id=int(row["mexc_protection_order_id"]) if row.get("mexc_protection_order_id") is not None else None,
            breach_100_at=row.get("breach_100_at"),
            breach_200_at=row.get("breach_200_at"),
            breach_300_at=row.get("breach_300_at"),
            breach_400_at=row.get("breach_400_at"),
            entry_fee_usdt=float(row.get("entry_fee_usdt") or 0.0),
            exit_fee_usdt=float(row.get("exit_fee_usdt") or 0.0),
            mexc_position_id=int(row["mexc_position_id"]) if row["mexc_position_id"] is not None else None,
            mexc_open_order_id=int(row["mexc_open_order_id"]) if row["mexc_open_order_id"] is not None else None,
            metadata=metadata,
        )
