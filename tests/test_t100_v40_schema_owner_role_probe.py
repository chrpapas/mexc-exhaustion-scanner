"""Offline fail-closed owner privilege audit. No Render/DB writes or credentials."""
import pytest

from app.t100_v40_schema_owner_role_probe import (
    CANDIDATE_ROLE,OWNER_SQL,RUNTIME_ROLE,observe_owner_role_catalog,
)


def record(**changed):
    row={
        "database_name":"mexc_exhaustion",
        "candidate_role_exists":True,
        "runtime_role_exists":True,
        "candidate_can_create_public":True,
        "checkpoint_table_owner":CANDIDATE_ROLE,
        "revision_table_owner":CANDIDATE_ROLE,
        "candidate_can_reference_slot_reservations":True,
    }
    row.update(changed)
    return row


class FakePool:
    def __init__(self,row,*,error=False):
        self.row=row
        self.error=error
        self.calls=[]
        self.transactions=[]

    def acquire(self):return self
    def transaction(self,*,isolation,readonly):
        self.transactions.append((isolation,readonly))
        return self
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False
    async def fetchrow(self,sql,*args):
        self.calls.append((sql,args))
        if self.error:
            raise RuntimeError("PASSWORD_AND_DSN_MUST_NEVER_BE_LOGGED")
        return self.row


@pytest.mark.asyncio
async def test_actual_owner_candidate_directly_owns_both_tables_and_create():
    pool=FakePool(record())
    report=await observe_owner_role_catalog(pool)
    assert report.catalog_observed is True
    assert report.database_identity_verified is True
    assert report.owner_role_permissions_sufficient_on_catalog is True
    assert report.authenticated_as_owner is False
    assert report.production_ddl_applied is False
    assert report.live_authorization is False
    assert report.execution_ready_certified is False
    assert report.database_writes==report.mexc_posts==0
    assert pool.transactions==[("repeatable_read",True)]
    assert pool.calls==[(OWNER_SQL,(CANDIDATE_ROLE,RUNTIME_ROLE))]
    for fragment in ["UPDATE ","DELETE ","INSERT ","ALTER ","GRANT "]:
        assert fragment not in OWNER_SQL


@pytest.mark.asyncio
@pytest.mark.parametrize("change",[
    {"candidate_role_exists":False},
    {"runtime_role_exists":False},
    {"candidate_can_create_public":False},
    {"candidate_can_reference_slot_reservations":False},
    {"checkpoint_table_owner":"different_migration_admin"},
    {"revision_table_owner":"different_migration_admin"},
    {"checkpoint_table_owner":None},
    {"revision_table_owner":None},
    {"database_name":"wrong_db"},
])
async def test_any_owner_mismatch_means_not_sufficient(change):
    report=await observe_owner_role_catalog(FakePool(record(**change)))
    assert report.catalog_observed
    assert not report.owner_role_permissions_sufficient_on_catalog
    assert not report.authenticated_as_owner


@pytest.mark.asyncio
async def test_missing_catalog_or_sql_failure_remains_unknown_with_no_secrets():
    for pool in [FakePool(None),FakePool(record(),error=True),
                 FakePool(record(runtime_role_exists="true"))]:
        report=await observe_owner_role_catalog(pool)
        assert report.catalog_observed is False
        assert report.owner_role_permissions_sufficient_on_catalog is False
        assert report.candidate_role_exists is None
        assert "PASSWORD" not in report.status
        assert report.database_writes==report.mexc_posts==0


@pytest.mark.asyncio
async def test_missing_restricted_pool_never_runs_sql():
    report=await observe_owner_role_catalog(object())
    assert report.catalog_observed is False
    assert report.status=="V40_OWNER_ROLE_READONLY_POOL_REQUIRED"
