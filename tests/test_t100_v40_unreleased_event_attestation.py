"""Unreleased evidence bridge cannot turn requests into fake Discord fills."""
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_fill_ledger import VerifiedOrderFillSet
from app.t100_live_v40_protected_finalization import PreparedProtectedEntry
from app.t100_v40_unreleased_event_attestation import (
    verified_entry_and_native_sl_events,
    verified_completed_stop_and_pnl_events,
)
from app.t100_v40_unreleased_trade_events import V40EventInvalid


def prepared_fixture():
    cp = PositionCheckpoint(
        symbol="EXAMPLE_USDT", external_oid="v40fixture",
        exchange_order_id="90", position_id="10", contracts=D("1"),
        entry_price=D("100"), stop_order_id="92",
        stop_price=D("175"), best_profit_pct=D("0"), armed=False,
        lifecycle_state="OPEN", checkpoint_version=1,
    )
    fills = VerifiedOrderFillSet(
        order_id="90", position_id="10", symbol="EXAMPLE_USDT",
        side="OPEN_SHORT", order_state=3, deal_volume=D("1"),
        average_price=D("100"), fee_currency="USDT", fills=(),
        fill_digest_sha256="abc", final_order_fill_history_complete=True,
        strategy_fully_filled_order_complete=True,
    )
    p = PreparedProtectedEntry(
        stage=SimpleNamespace(signal_id=7),
        intent=SimpleNamespace(signal_id=7, symbol=cp.symbol, external_oid=cp.external_oid),
        order_id="90", position_id="10", checkpoint=cp, fill_set=fills,
    )
    return p, cp


def test_entry_discord_requires_durable_commit_and_restarted_get():
    p, cp = prepared_fixture()
    with pytest.raises(V40EventInvalid, match="EVIDENCE_MISSING"):
        verified_entry_and_native_sl_events(
            prepared=p, persisted_checkpoint=cp, current_native_sl_get={},
            post_commit_restart_recovered=False,
            signed_get_account_complete=True,
            observed_at=datetime.now(timezone.utc),
        )


def test_entry_discord_requires_actual_complete_fills():
    p, cp = prepared_fixture()
    with pytest.raises(V40EventInvalid, match="ENTRY_OR_FILL_DRIFT"):
        verified_entry_and_native_sl_events(
            prepared=p, persisted_checkpoint=cp, current_native_sl_get={"id":"92"},
            post_commit_restart_recovered=True,
            signed_get_account_complete=True,
            observed_at=datetime.now(timezone.utc),
        )


def test_exit_discord_cannot_claim_sl_trigger_without_signed_finality():
    with pytest.raises(V40EventInvalid, match="NOT_FINAL"):
        verified_completed_stop_and_pnl_events(
            signal_id=7, position_id="10", symbol="EXAMPLE_USDT",
            close_provenance=None, close_fills=None, accounting=None,
            signed_account_flat_twice=False, durable_checkpoint_closed=False,
            observed_at=datetime.now(timezone.utc),
        )
