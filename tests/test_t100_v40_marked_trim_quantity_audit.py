"""Independent 200bps trim request/lot quantity audit; synthetic inputs only."""
import importlib.util
import tempfile
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT=Path(__file__).resolve().parents[1]
FILE=ROOT/"docs/evidence/issue-14/rebuild_marked_equity_drawdown.py"
spec=importlib.util.spec_from_file_location("v40_marked_trim_module",FILE)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

from scripts.v40_risk_trim_audit import planned_trims as archived_pure_planner


def test_independent_priorclose_trim_plans_equal_existing_reference():
    for equity, marked in (
        (2000,{"A_USDT":100,"B_USDT":200}),
        (2000,{"A_USDT":2000,"B_USDT":1700,"C_USDT":100}),
        (50,{"A_USDT":100,"B_USDT":300}),
        (0,{"A_USDT":100,"B_USDT":300}),
    ):
        for cost in (100,200):
            candidate=module.prior_close_trim_plans(equity,marked,cost)
            expected=archived_pure_planner(equity,marked,cost)
            assert candidate==expected
    with pytest.raises(ValueError,match="INVALID_TRIM_ACCOUNT_SNAPSHOT"):
        module.prior_close_trim_plans("NaN",{"A_USDT":100},200)


def test_trim_model_known_lots_and_causal_prior30_capacity():
    spec={"contractSize":"0.01","volUnit":"1","minVol":"1"}
    basis,requested,known=module.model_risk_trim_quantity(
        original_basis=100,entry_price=100,opening_price=120,
        plan_fraction=".40",prior_turnover=4000,contract_spec=spec)
    assert (basis,requested,known)==(D(40),D(40),True)
    basis,requested,known=module.model_risk_trim_quantity(
        original_basis=100,entry_price=100,opening_price=120,
        plan_fraction=".40",prior_turnover=400,contract_spec=spec)
    assert (basis,requested,known)==(D(16),D(40),True)
    assert basis*D("1.20")<=D(20)


def test_trim_model_avoids_small_residual_lot():
    spec={"contractSize":".01","volUnit":"1","minVol":"5"}
    basis,requested,known=module.model_risk_trim_quantity(
        original_basis=100,entry_price=100,opening_price=100,
        plan_fraction=".98",prior_turnover=10000,contract_spec=spec)
    assert basis==95 and requested==98 and known


def test_trim_model_unknown_spec_explicit_reference_passthrough():
    basis,request,known=module.model_risk_trim_quantity(
        original_basis=100,entry_price=100,opening_price=120,
        plan_fraction=".40",prior_turnover=400,contract_spec=None)
    assert basis==D(20)*D(100)/D(120)
    assert request==40 and known is False


def test_trim_model_cannot_use_invalid_inputs():
    spec={"contractSize":".01","volUnit":"1","minVol":"1"}
    for bad in (D("NaN"),D("-1"),D("1.1"),D(0)):
        with pytest.raises(ValueError,match="INVALID_TRIM_QUANTITY_INPUT"):
            module.model_risk_trim_quantity(
                original_basis=100,entry_price=100,opening_price=120,
                plan_fraction=bad,prior_turnover=400,contract_spec=spec)
    with pytest.raises(ValueError,match="TRIM_ORIGINAL_LOT_INVENTORY_UNVERIFIED"):
        module.model_risk_trim_quantity(
            original_basis="100.4",entry_price=100,opening_price=120,
            plan_fraction=".4",prior_turnover=4000,contract_spec=spec)


def test_opt_in_trim_mode_requires_current_lot_snapshot():
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)
        with pytest.raises(ValueError,match="TRIM_REPLAY_REQUIRES_CURRENT_REFERENCE_SPECS"):
            module.audit(root,{"historical":root},audit_trims=True,
                         entry_specs=None)


def test_non_authorizing_model_remains_ledger_conditioned():
    assert "audit_trims" in module.audit.__code__.co_varnames
    assert "from scripts.v40_risk_trim_audit import planned_trims" not in FILE.read_text()
