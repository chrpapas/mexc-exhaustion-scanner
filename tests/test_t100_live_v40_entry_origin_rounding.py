"""One V40 externalOid and executable contract notional across all boundaries."""
from decimal import Decimal as D
import pytest

from app.t100_live_execution import (
    EntryIntent, deterministic_t100_entry_oid,
)
from app.t100_live_four_slot_attempt_journal import _validate
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_leader_fencing import LeaderLease


def test_reservation_oid_equals_real_intent_oid_without_salt_or_retry():
    oid=deterministic_t100_entry_oid(3001,"FROZEN_USDT")
    intent=EntryIntent.build(
        signal_id=3001,symbol="FROZEN_USDT",
        reference_price="2",notional_usdt="500",
        contract_size="1",volume_step="1",
        min_volume="1",max_volume="1000",
        price_tick="0.001",
    )
    assert oid==intent.external_oid
    assert oid.startswith("t100v1e")
    assert len(oid)==27
    assert deterministic_t100_entry_oid(3001,"FROZEN_USDT")==oid
    assert deterministic_t100_entry_oid(3002,"FROZEN_USDT")!=oid


def test_fractional_target_is_rounded_down_into_exact_journal_notional():
    intent=EntryIntent.build(
        signal_id=3003,symbol="FROZEN_USDT",
        reference_price="3",notional_usdt="100",
        contract_size="2",volume_step="1",
        min_volume="1",max_volume="1000",
        price_tick="0.1",
    )
    assert intent.volume_contracts==D("16")
    assert intent.notional_usdt==D("96")
    assert intent.notional_usdt==(
        intent.volume_contracts*D("2")*intent.reference_price
    )
    assert intent.stop_price==D("5.3")


def test_exact_integer_lot_preserves_strategy_notional():
    intent=EntryIntent.build(
        signal_id=3004,symbol="FROZEN_USDT",
        reference_price="2",notional_usdt="500",
        contract_size="1",volume_step="1",
        min_volume="1",max_volume="1000",
        price_tick="0.001",
    )
    assert intent.notional_usdt==D("500")
    assert intent.volume_contracts==D("250")


def test_origin_invalid_inputs_rejected_not_reformatted():
    for signal_id,symbol in [(0,"FROZEN_USDT"),(True,"FROZEN_USDT"),
                             (100,"BTC_USDT"),(100,"eth_usdt"),
                             (100,"FROZEN/USDT")]:
        with pytest.raises(ValueError):
            deterministic_t100_entry_oid(signal_id,symbol)


def test_contract_lot_below_exchange_min_rejected_without_optimistic_topup():
    with pytest.raises(ValueError,match="below minimum"):
        EntryIntent.build(
            signal_id=3005,symbol="FROZEN_USDT",
            reference_price="3",notional_usdt="5",
            contract_size="2",volume_step="1",
            min_volume="1",max_volume="1000",
            price_tick="0.1",
        )
