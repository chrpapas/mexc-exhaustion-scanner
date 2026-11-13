"""Exact, serializable V40 emergency close SQL claim tests, offline."""
import asyncio
from decimal import Decimal as D
import pytest

from app.t100_v40_emergency_close_claims import (
    V40PostgresEmergencyCloseClaims,_INSERT,_READ_LINKED,
)
from app.t100_v40_emergency_close import V40EmergencyCloseHalted
from tests.test_t100_v40_emergency_close import target


class Tx:
    def __init__(self,db):
        self.db=db
    async def __aenter__(self):
        self.db.events.append("SERIALIZABLE_BEGIN")
        return self
    async def __aexit__(self,*args):
        self.db.events.append("SERIALIZABLE_END")


class Pool:
    def __init__(self,linked=None):
        self.events=[]
        self.linked=linked
        self.inserted=False
        self.fail_at=None
        self.priv_ok=True
        self.pending=0
    def acquire(self):
        self.events.append("ACQUIRE")
        return self
    async def __aenter__(self):
        return self
    async def __aexit__(self,*a):
        self.events.append("RELEASE")
    def transaction(self,**kwargs):
        assert kwargs=={"isolation":"serializable"}
        return Tx(self)
    async def fetchrow(self,sql,*args):
        self.events.append("READ")
        if self.fail_at=="READ":raise RuntimeError("SECRET_DB_DSN")
        if "has_table_privilege" in sql:
            return {
                "select_ok":True,
                "insert_ok":self.priv_ok,
                "mutation_ok":False,
            }
        assert "FROM public.t100_live_position_checkpoints" in sql
        assert args==(9801,)
        return self.linked
    async def fetchval(self,sql,*args):
        if self.fail_at=="ANY":raise TimeoutError("SECRET_SQL_OWNER_PASS")
        if "current_database()" in sql:
            return "mexc_exhaustion"
        if "current_user" in sql:
            return "t100_live_executor_runtime"
        if "transaction_isolation" in sql:
            return "serializable"
        if "transaction_read_only" in sql:
            return "off"
        if "pg_advisory_xact_lock" in sql:
            self.events.append("ACCOUNT_LOCK")
            return None
        if "SELECT COUNT(*)" in sql:
            return self.pending
        if "INSERT INTO public.t100_live_v40_emergency_close_claims" in sql:
            self.events.append("CLAIM_INSERT_ATTEMPT")
            assert self.events.index("ACCOUNT_LOCK")<self.events.index(
                "CLAIM_INSERT_ATTEMPT"
            )
            assert args==(9801,"t100v1e"+"a"*20,target().close_external_oid,
                          "739206374277809664","DOGE_USDT",
                          D("1"),D("10"),1,"NATIVE_STOP_MISSING_DOUBLE_GET")
            if self.inserted:
                return None
            self.inserted=True
            return 9801
        raise AssertionError("unexpected SQL")


def linked():
    return {
        "position_id":9801,
        "symbol":"DOGE_USDT",
        "external_oid":"t100v1e"+"a"*20,
        "exchange_order_id":"739206374277809664",
        "contracts":D("1"),"stop_order_id":"13511",
        "checkpoint_version":1,"lifecycle_state":"OPEN",
        "intent_symbol":"DOGE_USDT",
        "intent_oid":"t100v1e"+"a"*20,
        "intent_state":"ACK",
        "intent_order_id":"739206374277809664",
        "intent_contracts":D("1"),"intent_notional":D("10"),
        "intent_signal_id":727,
        "slot_state":"PROTECTED","slot_signal_id":727,
        "slot_oid":"t100v1e"+"a"*20,
    }


def invoke(pool):
    return asyncio.run(
        V40PostgresEmergencyCloseClaims(pool).reserve_one(target())
    )


def test_one_claim_under_shared_account_lock_and_repeated_attempt_denied():
    pool=Pool(linked())
    assert invoke(pool) is True
    assert pool.inserted
    assert pool.events.index("ACCOUNT_LOCK")<pool.events.index("CLAIM_INSERT_ATTEMPT")
    assert "SERIALIZABLE_END" in pool.events
    assert invoke(pool) is False
    assert pool.events.count("CLAIM_INSERT_ATTEMPT")==2


@pytest.mark.parametrize("diff",[
    {"position_id":9802},{"lifecycle_state":"CLOSING"},
    {"intent_state":"UNKNOWN"},{"intent_notional":D("12.51")},
    {"intent_order_id":"123"},{"slot_state":"RESERVED"},
    {"slot_signal_id":728},{"checkpoint_version":2},
])
def test_mismatched_sql_owner_or_unresolved_history_prevents_claim(diff):
    pool=Pool({**linked(),**diff})
    with pytest.raises(V40EmergencyCloseHalted,match="ORIGINAL_OWNER_DRIFT"):
        invoke(pool)
    assert not pool.inserted


def test_pending_order_or_stop_prevents_claim():
    pool=Pool(linked());pool.pending=1
    with pytest.raises(V40EmergencyCloseHalted,match="UNKNOWN_ENTRY_OR_STOP_PENDING"):
        invoke(pool)
    assert not pool.inserted


def test_role_cannot_update_delete_or_truncate_emergency_history():
    pool=Pool(linked());pool.priv_ok=False
    with pytest.raises(V40EmergencyCloseHalted,match="PRIVILEGES_INVALID"):
        invoke(pool)
    assert not pool.inserted


def test_db_error_is_redacted_and_post_cannot_start():
    pool=Pool(linked());pool.fail_at="ANY"
    with pytest.raises(V40EmergencyCloseHalted,match="DB_CLAIM_UNCERTAIN"):
        invoke(pool)
    assert not pool.inserted
