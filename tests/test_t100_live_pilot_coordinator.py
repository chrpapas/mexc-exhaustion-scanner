"""The retired one-slot prototype must never reach any execution dependency."""
import unittest
from unittest.mock import AsyncMock

from app.t100_live_pilot_coordinator import TinyPilotCoordinator, TinyPilotHalted
from app.t100_v40_admission import V40Admission


class RetiredPilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_even_positive_readiness_cannot_reactivate_retired_path(self):
        gateway = AsyncMock()
        supplier = AsyncMock(return_value={"dynamic_readiness_verified": True})
        coordinator = TinyPilotCoordinator(gateway=gateway, dynamic_readiness_supplier=supplier)
        for admission in (V40Admission(action="ADMIT", reason="OK", modeled_notional_usdt=100),
                          V40Admission(action="SKIP", reason="BLOCKED"), None):
            with self.subTest(admission=admission):
                with self.assertRaisesRegex(TinyPilotHalted, "PILOT_COORDINATOR_RETIRED_FULL_V40_REQUIRED"):
                    await coordinator.submit_one(
                        intent=object(), admission=admission, permit=object(),
                        code_revision_report={"code_revision_verified": True},
                        strategy_parity_report={"strategy_parity_verified": True},
                        ws_pre_pilot_report={"private_ws_pre_pilot_contract_verified": True},
                        market=object(),
                    )
                supplier.assert_not_called()
                gateway.send_one.assert_not_called()
