"""Second one-time V40 critical-incident recovery, offline and fail-closed."""
import asyncio
import pytest

import app.t100_v40_guardian_reviewed_generation as m
from tests.test_t100_v40_canary_durable_guardian import env, flat, exposed


class DB:
    def __init__(self):
        self.states = {
            m.LEGACY_SCOPE: {
                "last_status": m.GOOD_STATUS,
                "last_alert": False,
                "critical_latched": True,
            },
            m.REVIEWED_SCOPE: {
                "last_status": m.GOOD_STATUS,
                "last_alert": False,
                "critical_latched": True,
                "revision": 22,
                "recently_healthy": True,
            },
            m.RECOVERY_SCOPE_002: None,
        }
        self.incidents = {
            m.LEGACY_SCOPE: {
                "critical_total": 1, "forbidden_critical": 0, "undelivered": 0,
            },
            m.REVIEWED_SCOPE: {
                "critical_total": 1, "forbidden_critical": 0, "undelivered": 0,
            },
        }
        self.saved = []
        self.queries = []

    async def fetchrow(self, sql, *args):
        self.queries.append(sql.strip())
        if "INSERT INTO public.t100_guardian_incident_state" in sql:
            assert args == (m.RECOVERY_SCOPE_002, m.AUDIT_STATUS, False)
            self.saved.append(args)
            return {"revision": 1, "critical_latched": False}
        if "FROM public.t100_guardian_incident_state" in sql:
            return self.states[args[0]]
        if "FROM public.t100_guardian_incident_outbox" in sql:
            assert args[1] == m.HISTORIC_CRITICAL
            return self.incidents[args[0]]
        raise AssertionError("unexpected SQL")

    async def fetch(self, *args):
        return []

    async def fetchval(self, *args):
        return 0


async def select(db, samples=None):
    events = []
    checks = list(samples if samples is not None else [flat(), flat()])

    async def observe(_):
        events.append("SIGNED_GET_AND_LEDGER")
        outcome = checks.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def pause(t):
        assert t == 3.0
        events.append("PAUSE")

    result = await m.select_second_reviewed_scope(
        db, env=env(), observe=observe, pause=pause,
    )
    return result, events


def test_creates_one_epoch_after_both_delivered_incidents_and_fresh_two_get():
    db = DB()
    result, events = asyncio.run(select(db))
    assert result == (
        m.RECOVERY_SCOPE_002, "NEW_RECOVERY_002_AFTER_DOUBLE_GET",
    )
    assert events == [
        "SIGNED_GET_AND_LEDGER", "PAUSE", "SIGNED_GET_AND_LEDGER",
    ]
    assert len(db.saved) == 1
    assert all(db.states[s]["critical_latched"] is True
               for s in (m.LEGACY_SCOPE, m.REVIEWED_SCOPE))
    assert all(db.incidents[s]["critical_total"] == 1
               for s in (m.LEGACY_SCOPE, m.REVIEWED_SCOPE))
    assert all("DELETE FROM" not in q and "TRUNCATE" not in q
               for q in db.queries)


@pytest.mark.parametrize("scope", [m.LEGACY_SCOPE, m.REVIEWED_SCOPE])
@pytest.mark.parametrize("fault", [
    {"critical_total": 0}, {"critical_total": 2},
    {"forbidden_critical": 1}, {"undelivered": 1},
])
def test_any_missing_duplicate_forbidden_or_undelivered_incident_halts(scope, fault):
    db = DB()
    db.incidents[scope].update(fault)
    with pytest.raises(m.V40GuardianReviewedGenerationHalted,
                       match="UNREVIEWED_ALERTS"):
        asyncio.run(select(db))
    assert not db.saved


@pytest.mark.parametrize("fault", [
    {"last_status": "OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"},
    {"last_alert": True}, {"critical_latched": False},
    {"recently_healthy": False}, {"revision": 1},
])
def test_reviewed_001_must_still_be_recently_flat_and_latched(fault):
    db = DB()
    db.states[m.REVIEWED_SCOPE].update(fault)
    with pytest.raises(m.V40GuardianReviewedGenerationHalted,
                       match="LEGACY_NOT_RECOVERED"):
        asyncio.run(select(db))
    assert not db.saved


@pytest.mark.parametrize("bad", [
    exposed(),
    RuntimeError("SENSITIVE_API_KEY"),
])
def test_unknown_position_or_failed_fresh_get_cannot_open_epoch_002(bad):
    db = DB()
    with pytest.raises(m.V40GuardianReviewedGenerationHalted,
                       match="FRESH_FLAT_UNVERIFIED") as exc:
        asyncio.run(select(db, [flat(), bad]))
    assert "SENSITIVE" not in str(exc.value)
    assert not db.saved


def test_existing_latched_002_never_reinitializes():
    db = DB()
    db.states[m.RECOVERY_SCOPE_002] = {
        "last_status": m.HISTORIC_CRITICAL,
        "last_alert": True,
        "critical_latched": True,
    }
    result, events = asyncio.run(select(db))
    assert result == (m.RECOVERY_SCOPE_002, "EXISTING_RECOVERY_002_SCOPE")
    assert not events and not db.saved


def test_unrecognized_002_state_halts():
    db = DB()
    db.states[m.RECOVERY_SCOPE_002] = {
        "last_status": "UNRECOGNIZED",
        "last_alert": False,
        "critical_latched": False,
    }
    with pytest.raises(m.V40GuardianReviewedGenerationHalted,
                       match="REVIEWED_STATE_INVALID"):
        asyncio.run(select(db))
    assert not db.saved


def test_scopes_are_distinct_fixed_hashes_and_gate_selects_002():
    assert len({m.LEGACY_SCOPE, m.REVIEWED_SCOPE,
                m.ACTIVE_REVIEWED_SCOPE}) == 3
    assert m.ACTIVE_REVIEWED_SCOPE == m.RECOVERY_SCOPE_002
