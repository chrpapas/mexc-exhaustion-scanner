"""Offline symbol/account execution eligibility tests."""
import unittest

from app.mexc_trade import ContractSpec
from app.t100_live_symbol_eligibility import (
    SymbolEligibilityHalted,
    assess_symbol_execution_eligibility,
)


def spec(**kw):
    data=dict(
        symbol="SIREN_USDT",
        contract_size=1.0,
        vol_unit=1.0,
        min_vol=1.0,
        max_vol=1000.0,
        api_allowed=True,
        position_open_type=3,
    )
    data.update(kw)
    return ContractSpec(**data)


class SymbolEligibilityTests(unittest.TestCase):
    def test_api_allowed_sane_spec_and_hedge_mode_pass(self):
        r=assess_symbol_execution_eligibility(
            symbol="SIREN_USDT",
            contract_spec=spec(),
            position_mode=1,
        )
        self.assertTrue(r.eligible)
        self.assertTrue(r.api_allowed)
        self.assertTrue(r.hedge_mode_verified)
        self.assertTrue(r.contract_spec_sane)
        self.assertFalse(r.live_authorization)

    def test_api_disallowed_blocks(self):
        r=assess_symbol_execution_eligibility(
            symbol="SIREN_USDT",
            contract_spec=spec(api_allowed=False),
            position_mode=1,
        )
        self.assertFalse(r.eligible)

    def test_one_way_or_unknown_mode_blocks(self):
        for mode in (2,None,"",True):
            with self.subTest(mode=mode):
                r=assess_symbol_execution_eligibility(
                    symbol="SIREN_USDT",
                    contract_spec=spec(),
                    position_mode=mode,
                )
                self.assertFalse(r.eligible)

    def test_bad_contract_limits_block(self):
        for bad in (
            spec(contract_size=0),
            spec(vol_unit=0),
            spec(min_vol=0),
            spec(max_vol=0),
            spec(min_vol=10,max_vol=1),
        ):
            with self.subTest(spec=bad):
                r=assess_symbol_execution_eligibility(
                    symbol="SIREN_USDT",
                    contract_spec=bad,
                    position_mode=1,
                )
                self.assertFalse(r.eligible)

    def test_symbol_identity_mismatch_halts(self):
        with self.assertRaisesRegex(
            SymbolEligibilityHalted,
            "IDENTITY_INVALID",
        ):
            assess_symbol_execution_eligibility(
                symbol="OTHER_USDT",
                contract_spec=spec(),
                position_mode=1,
            )


if __name__=="__main__":
    unittest.main()
