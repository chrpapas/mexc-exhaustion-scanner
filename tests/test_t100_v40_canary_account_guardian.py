"""Independent V40 standby guardian: mock-only signed GET, no orders."""
import asyncio
import json

import pytest

import app.t100_v40_canary_account_guardian as m


EMPTY={
    "sumCount":0,"stopOrderCount":0,"limitOrderCount":0,
    "planOrderCount":0,"trackOrderCount":0,
}
ENV={
    "T100_V40_CANARY_GUARDIAN_ENABLED":"true",
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
    "T100_GUARDIAN_OBSERVER_ENABLED":"true",
    "T100_V40_GUARDIAN_ACCOUNT_SCOPE":"V40_DEDICATED",
    "T100_V40_GUARDIAN_READONLY_API_KEY":"TEST_GUARDIAN_READONLY_KEY_123456",
    "T100_V40_GUARDIAN_READONLY_API_SECRET":"TEST_GUARDIAN_READONLY_SECRET_123456",
    "T100_V40_CANARY_GUARDIAN_INTERVAL_SECONDS":"5",
}


class Fake:
    def __init__(self,steps=None,fail=False):
        self.calls=[]
        self.steps=steps or [
            ([],[],dict(EMPTY),1),
            ([],[],dict(EMPTY),1),
        ]
        self.fail=fail
        self.k=0
    async def _item(self,index,name):
        self.calls.append(name)
        if self.fail:
            raise RuntimeError("SENSITIVE_API_SECRET")
        step=self.steps[self.k][index]
        if index==3:
            self.k+=1
        return step
    async def raw_open_positions(self):
        return await self._item(0,"positions")
    async def raw_open_stops(self):
        return await self._item(1,"stops")
    async def raw_open_order_counts(self):
        return await self._item(2,"counts")
    async def raw_position_mode(self):
        return await self._item(3,"mode")


def obs(p):
    return asyncio.run(m.observe_v40_flat_guardian(p))


def test_double_get_empty_hedge_account_is_standby_but_not_trade_authorized():
    p=Fake()
    r=obs(p)
    assert r.status=="V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING"
    assert r.monitored_account_flat is True
    assert r.inventory_two_signed_get_bookends is True
    assert r.hedge_mode_verified is True
    assert r.incident_alert is False
    assert r.running_guardian_certified is False
    assert r.independent_durable_alert_delivery is False
    assert r.entry_allowed is False and r.live_authorization is False
    assert r.mexc_posts==r.database_writes==0
    assert p.calls==[
        "positions","stops","counts","mode",
        "positions","stops","counts","mode",
    ]


@pytest.mark.parametrize("fault,field",[
    (([{"positionId":123,"symbol":"TEST_USDT"}],[],dict(EMPTY),1),"unexpected_positions"),
    (([],[{"id":333,"symbol":"TEST_USDT"}],dict(EMPTY),1),"unexpected_stops"),
    (([],[],{**EMPTY,"sumCount":1,"limitOrderCount":1},1),"unexpected_orders"),
])
def test_nonflat_or_pending_order_never_adopted_as_managed(fault,field):
    p=Fake(steps=[fault,fault])
    r=obs(p)
    assert r.status=="V40_GUARDIAN_NONFLAT_OR_PENDING_ORDER_UNKNOWN_OWNER_HALT"
    assert r.incident_alert is True
    assert getattr(r,field)==1
    assert r.running_guardian_certified is False
    assert not r.live_authorization and not r.emergency_close_allowed
    assert r.mexc_posts==0


@pytest.mark.parametrize("s1,s2",[
    (([],[],dict(EMPTY),1),([],[],dict(EMPTY),2)),
    (([],[],dict(EMPTY),1),([],[{"id":1}],dict(EMPTY),1)),
    (([],[],dict(EMPTY),1),([],[],{**EMPTY,"sumCount":1,"limitOrderCount":1},1)),
])
def test_drift_between_signed_get_bookends_is_critical(s1,s2):
    r=obs(Fake(steps=[s1,s2]))
    assert r.status=="V40_GUARDIAN_ACCOUNT_SHAPE_OR_HEDGE_UNVERIFIED_HALT" or (
        r.status=="V40_GUARDIAN_ACCOUNT_BOOKENDS_DRIFT_HALT"
    )
    assert r.incident_alert and not r.monitored_account_flat


def test_invalid_counts_and_failed_get_are_redacted():
    r=obs(Fake(steps=[([],[],{"sumCount":0},1)]*2))
    assert r.status=="V40_GUARDIAN_ACCOUNT_SHAPE_OR_HEDGE_UNVERIFIED_HALT"
    err=obs(Fake(fail=True))
    assert err.status=="V40_GUARDIAN_SIGNED_GET_UNAVAILABLE_HALT"
    assert "SENSITIVE" not in str(err)


@pytest.mark.parametrize("fault",[
    {"T100_V40_CANARY_GUARDIAN_ENABLED":"false"},
    {"T100_EXECUTION_MODE":"live"},
    {"T100_LIVE_ORDER_API_ENABLED":"true"},
    {"T100_LIVE_KILL_SWITCH":"false"},
    {"T100_GUARDIAN_OBSERVER_ENABLED":"false"},
    {"T100_V40_GUARDIAN_ACCOUNT_SCOPE":"BOXWAVE"},
    {"T100_V40_GUARDIAN_READONLY_API_KEY":""},
    {"T100_V40_GUARDIAN_READONLY_API_SECRET":""},
    {"T100_V40_TRADER_API_KEY":ENV["T100_V40_GUARDIAN_READONLY_API_KEY"]},
])
def test_separate_readonly_key_and_paper_kill_required(fault):
    with pytest.raises(m.V40CanaryGuardianHalted):
        m.verify_guardian_environment({**ENV,**fault})


def test_single_run_observes_and_halts_as_soon_as_account_nonflat():
    logs=[]
    events=[]
    async def observe(_):
        events.append("GET")
        return m.V40CanaryGuardianHeartbeat(
            "V40_GUARDIAN_NONFLAT_OR_PENDING_ORDER_UNKNOWN_OWNER_HALT",
            unexpected_positions=1,
        )
    async def pause(_):
        events.append("sleep")
    out=asyncio.run(m.run_v40_guardian(
        ENV,observe=observe,pause=pause,
        emit=logs.append,max_cycles=100,
    ))
    assert out==2 and events==["GET"]
    assert len(logs)==1
    assert json.loads(logs[0])["unexpected_positions"]==1
    assert json.loads(logs[0])["entry_allowed"] is False


def test_observer_errors_never_suppress_critical_alert_or_log_credentials():
    output=[]
    async def bad(_):
        raise RuntimeError("SECRET_DB_DSN")
    out=asyncio.run(m.run_v40_guardian(
        ENV,observe=bad,emit=output.append,max_cycles=1,
    ))
    assert out==2
    assert json.loads(output[0])["status"]=="V40_GUARDIAN_OBSERVER_ERROR_OR_TIMEOUT_HALT"
    assert "SECRET_DB_DSN" not in output[0]


def test_standby_continues_one_get_cycle_per_iteration_no_posts():
    events=[]
    async def good(_):
        events.append("GET")
        return m.V40CanaryGuardianHeartbeat(
            "V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING",
            monitored_account_flat=True,inventory_two_signed_get_bookends=True,
            hedge_mode_verified=True,incident_alert=False,
        )
    async def pause(t):
        assert t==5
        events.append("sleep")
    result=asyncio.run(m.run_v40_guardian(
        ENV,observe=good,pause=pause,
        max_cycles=3,emit=lambda _:None,
    ))
    assert result==0
    assert events==["GET","sleep","GET","sleep","GET"]
