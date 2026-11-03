"""Offline final live mutation permit boundary tests."""
import unittest

from app.t100_live_guards import (
    LIVE_CONFIRMATION,
    T100ExecutionControls,
)
from app.t100_live_leader_fencing import LeaderLeaseCoordinator
from app.t100_live_mutation_permit import (
    LiveMutationEvidence,
    LiveMutationPermitHalted,
    LiveMutationPermitIssuer,
)


class Store:
    def __init__(self):
        self.holder = None
        self.token = 0
        self.current = False
    async def acquire(self,name,holder,ttl):
        if self.current and self.holder != holder:
            return 0,False
        if self.holder != holder:
            self.token += 1
        self.holder=holder
        self.current=True
        return self.token,True
    async def renew(self,name,holder,token,ttl):
        return self.current and self.holder==holder and self.token==token
    async def assert_current(self,name,holder,token):
        return self.current and self.holder==holder and self.token==token
    async def release(self,name,holder,token):
        if not await self.assert_current(name,holder,token):
            return False
        self.current=False
        return True


def controls():
    return T100ExecutionControls.from_env({
        "T100_EXECUTION_MODE":"live",
        "T100_LIVE_ORDER_API_ENABLED":"true",
        "T100_LIVE_KILL_SWITCH":"false",
        "T100_LIVE_OPERATOR_CONFIRM":LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT":"100",
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT":"100",
        "T100_LIVE_MAX_DAILY_LOSS_USDT":"25",
        "T100_LIVE_LEVERAGE":"1",
    })


def evidence(**kw):
    data=dict(
        account_reconciled=True,
        current_inventory_complete=True,
        no_unresolved_order_intents=True,
        no_unresolved_stop_amendments=True,
        protected_shadow_soak_passed=True,
        durable_schema_applied=True,
        append_only_permissions_verified=True,
        explicit_pilot_authorization=True,
    )
    data.update(kw)
    return LiveMutationEvidence(**data)


class MutationPermitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store()
        self.leases=LeaderLeaseCoordinator(self.store)
        self.lease=await self.leases.acquire("instance-a")
        self.issuer=LiveMutationPermitIssuer(
            controls=controls(),
            lease_coordinator=self.leases,
        )

    async def test_current_repository_cannot_issue_live_permit(self):
        with self.assertRaisesRegex(
            LiveMutationPermitHalted,
            "LIVE_ADAPTER_NOT_CERTIFIED:NOT_IMPLEMENTED",
        ):
            await self.issuer.issue(
                lease=self.lease,
                evidence=evidence(),
            )

    async def test_adapter_status_blocks_before_other_evidence(self):
        with self.assertRaisesRegex(
            LiveMutationPermitHalted,
            "LIVE_ADAPTER_NOT_CERTIFIED",
        ):
            await self.issuer.issue(
                lease=self.lease,
                evidence=evidence(
                    explicit_pilot_authorization=False,
                ),
            )


if __name__=="__main__":
    unittest.main()
