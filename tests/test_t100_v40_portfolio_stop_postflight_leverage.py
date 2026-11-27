"""Portfolio CROSS leverage and native SL postflight: synthetic signed GETs.

This tests the existing durable stop completion pipeline with selected MEXC
leverage independent of the frozen V40 order notional. No real account I/O.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from app import t100_live_v40_stop_completion_handoff as m
from app import t100_live_v40_native_stop_recovery as auditor_module
from tests.test_t100_live_v40_stop_completion_handoff import (
    fixture,authorize_mock_only,
)


def permit(leverage=3,scope="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL"):
    return SimpleNamespace(leverage=leverage,scope=scope)


def setup(leverage=3):
    x=fixture()
    a,previous,trailing,handoff,journal,writer,venue,private=x
    private.pos_rows[0]["leverage"]=leverage
    venue.positions[0]["leverage"]=leverage
    return x


def invoke(x,*,p=None,portfolio_map=None):
    a,previous,trailing,handoff,*_=x
    return asyncio.run(handoff.commit_in_place_if_verified(
        amendment=a,previous=previous,trailing=trailing,
        permit=p if p is not None else permit(),
        portfolio_leverage_by_position=portfolio_map,
    ))


def test_production_release_still_denies_before_signed_get_and_db_write():
    x=setup(3)
    with pytest.raises(Exception):
        invoke(x,portfolio_map={"10":3})
    assert not x[7].calls
    assert not x[5].calls


def test_exact_3x_exchange_cross_can_complete_native_SL_and_durable_CAS(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=setup(3)
    result=invoke(x,portfolio_map={"10":3})
    assert result.status=="STOP_COMPLETION_JOURNAL_CHECKPOINT_PERSISTED_RESTART_REQUIRED"
    assert result.journal_checkpoint_committed is True
    assert result.needs_post_commit_independent_restart_check is True
    assert result.exchange_posts==0 and result.live_authorization is False
    assert len(x[5].calls)==1
    assert len(x[7].calls)==8


@pytest.mark.parametrize("badmap",[
    None,{},{"10":1},{"10":2},{"10":4},{"10":True},
    {"11":3},{"10":8},{"10":0},{"10":-1},
])
def test_wrong_or_missing_selected_cross_leverage_rejected_before_exchange_get(
    monkeypatch,badmap,
):
    authorize_mock_only(monkeypatch)
    x=setup(3)
    with pytest.raises(
        m.V40StopCompletionHalted,match="ENTRY_LEVERAGE_PROVENANCE_REQUIRED",
    ):
        invoke(x,portfolio_map=badmap)
    assert not x[7].calls and not x[5].calls


def test_live_signed_leverage_drift_cannot_commit_stop_journal(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=setup(3)
    x[7].pos_rows[0]["leverage"]=2
    result=invoke(x,portfolio_map={"10":3})
    assert result.status=="STOP_COMPLETION_SIGNED_POSITION_MODE_UNVERIFIED_FREEZE"
    assert not result.journal_checkpoint_committed
    assert not x[5].calls


def test_readonly_auditor_cross_leverage_drift_fails_before_write(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=setup(3)
    x[6].positions[0]["leverage"]=2
    result=invoke(x,portfolio_map={"10":3})
    assert result.status=="STOP_COMPLETION_WHOLE_ACCOUNT_RECOVERY_UNVERIFIED_FREEZE"
    assert not result.journal_checkpoint_committed
    assert not x[5].calls


def test_canary_cannot_inherit_portfolio_leverage_map(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=setup(1)
    with pytest.raises(
        m.V40StopCompletionHalted,match="CANARY_CANNOT_USE_PORTFOLIO_LEVERAGE",
    ):
        invoke(x,p=permit(1,scope="T100_V40_SUPERVISED_FIRST_SIGNAL"),
               portfolio_map={"10":1})
    assert not x[5].calls and not x[7].calls


def test_legacy_canary_1x_handoff_still_persists_without_portfolio_witness(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=setup(1)
    result=invoke(x,p=permit(1,scope="T100_V40_SUPERVISED_FIRST_SIGNAL"))
    assert result.journal_checkpoint_committed
    assert result.exchange_posts==0
    assert len(x[5].calls)==1


@pytest.mark.parametrize("bad",[
    {"10":True},{"10":6},{"10":0},{"10":"3"},
])
def test_auditor_invalid_portfolio_witness_rejected(monkeypatch,bad):
    x=setup(3)
    a=x[0]
    with pytest.raises(
        auditor_module.V40StopRecoveryHalted,
        match="CROSS_LEVERAGE_PROOF_INVALID",
    ):
        asyncio.run(x[3].auditor.observe(
            a,expected_cross_leverage_by_position=bad,
        ))



def test_extra_owned_leverage_key_fails_at_account_ownership_reconciliation(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=setup(3)
    out=invoke(x,portfolio_map={"10":3,"11":1})
    assert out.status=="STOP_COMPLETION_WHOLE_ACCOUNT_RECOVERY_UNVERIFIED_FREEZE"
    assert not out.journal_checkpoint_committed
    assert not x[5].calls


def test_auditor_extra_owned_leverage_key_fails_on_signed_ledger_ownership():
    x=setup(3)
    result=asyncio.run(x[3].auditor.observe(
        x[0],expected_cross_leverage_by_position={"10":3,"11":1},
    ))
    assert result.status=="V40_STOP_RECOVERY_UNOWNED_OR_UNPROTECTED_SLOT_FREEZE"
    assert x[5].calls==[]
