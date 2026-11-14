"""Guardian may observe 1–4 protected positions, but not authorize an entry."""
import asyncio

import pytest

from app.t100_v40_unreleased_nonflat_guardian_evidence import (
    PostgresV40NonflatGuardianEvidence,
)
from tests.test_t100_v40_guardian_post_gate import FakeConnection,FakePool


def check(conn):
    return asyncio.run(
        PostgresV40NonflatGuardianEvidence(FakePool(conn)).verify()
    )


def owned():
    conn=FakeConnection()
    conn.state["last_status"]="OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL"
    return conn


def test_healthy_owned_cross_native_sl_is_observation_only():
    conn=owned()
    out=check(conn)
    assert out.status=="V40_NONFLAT_GUARDIAN_PROTECTED_OBSERVER_FRESH_NONAUTHORIZING"
    assert out.guardian_protected_observation_fresh
    assert out.no_current_scope_critical_or_pending_incidents
    assert not out.exact_positions_independently_verified
    assert not out.cross_margin_and_liquidation_verified
    assert not out.exchange_stop_amendments_verified
    assert not out.next_entry_allowed and not out.live_authorization
    assert out.exchange_posts==out.database_writes==0
    assert len(conn.queries)==6
    assert all(not q.startswith(("UPDATE","INSERT","DELETE","TRUNCATE")) for q in conn.queries)


def test_flat_guardian_does_not_get_forged_nonflat_attestation():
    conn=FakeConnection()
    assert check(conn).status=="V40_NONFLAT_GUARDIAN_NOT_FRESH_OR_OWNED_HALT"


@pytest.mark.parametrize("field,value",[
    ("last_status","OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"),
    ("last_status","OBSERVER_V40_CANARY_FLAT_DOUBLE_GET"),
    ("critical_latched",True),
    ("last_alert",True),
    ("fresh",False),
    ("not_future",False),
    ("revision",1),
])
def test_sticky_critical_old_or_stale_nonflat_gate_halts(field,value):
    conn=owned()
    conn.state[field]=value
    out=check(conn)
    assert out.status=="V40_NONFLAT_GUARDIAN_NOT_FRESH_OR_OWNED_HALT"
    assert not out.guardian_protected_observation_fresh
    assert not out.next_entry_allowed


@pytest.mark.parametrize("field,value",[
    ("critical_total",1),("pending_total",1),
    ("critical_total",True),("pending_total",False),
])
def test_any_alert_or_wrong_counter_shape_halts(field,value):
    conn=owned()
    conn.outbox[field]=value
    out=check(conn)
    assert out.status=="V40_NONFLAT_GUARDIAN_ALERT_OR_LATCH_HALT"


@pytest.mark.parametrize("field,value",[
    ("role","postgres"),("db","unexpected"),("readonly","off"),
    ("isolation","read committed"),
])
def test_role_database_or_isolation_mismatch_rejected(field,value):
    conn=owned()
    setattr(conn,field,value)
    assert check(conn).status=="V40_NONFLAT_GUARDIAN_DB_ROLE_INVALID_HALT"


def test_database_failure_redacts_secrets_and_no_permit():
    class Broken:
        def acquire(self):
            raise RuntimeError("postgresql://SECRET")
    out=asyncio.run(PostgresV40NonflatGuardianEvidence(Broken()).verify())
    assert "postgresql://" not in repr(out)
    assert out.status=="V40_NONFLAT_GUARDIAN_DATABASE_UNAVAILABLE_HALT"
    assert not out.next_entry_allowed and not out.live_authorization
