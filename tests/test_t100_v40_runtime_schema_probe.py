"""Runtime schema inventory never grants live execution or changes SQL."""
import pytest

from app.t100_v40_runtime_schema_probe import (
    READINESS_SQL, observe_runtime_catalog,
)


class FakeAcquire:
    def __init__(self, row, *, fault=False):
        self.row = row
        self.fault = fault
        self.calls = []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def fetchrow(self, sql):
        self.calls.append(sql)
        if self.fault:
            raise RuntimeError("SECRET DB schema failure")
        return self.row


def row(**changes):
    result = dict(
        final_accounting=True, fill_sets=True, funding_sets=True,
        exit_cursor=True, at_entry_contract_specs=True,
        checkpoint_bar_observed=True,
    )
    result.update(changes)
    return result


@pytest.mark.asyncio
async def test_catalog_present_is_observation_not_live_certification():
    pool=FakeAcquire(row())
    result=await observe_runtime_catalog(pool)
    assert result.catalog_observed is True
    assert result.final_accounting_table_present is True
    assert result.exit_cursor_table_present is True
    assert result.at_entry_contract_specs_table_present is True
    assert not result.execution_ready_certified
    assert not result.live_authorization
    assert result.database_writes == result.mexc_posts == 0
    assert pool.calls == [READINESS_SQL]
    assert "UPDATE " not in READINESS_SQL
    assert "INSERT " not in READINESS_SQL


@pytest.mark.asyncio
async def test_missing_draft_schema_is_reported_without_faking_readiness():
    pool=FakeAcquire(row(
        final_accounting=False,exit_cursor=False,
        at_entry_contract_specs=False,
        checkpoint_bar_observed=False,
    ))
    result=await observe_runtime_catalog(pool)
    assert result.catalog_observed
    assert not result.final_accounting_table_present
    assert not result.exit_cursor_table_present
    assert not result.at_entry_contract_specs_table_present
    assert not result.checkpoint_bar_observed_supported
    assert not result.execution_ready_certified


@pytest.mark.asyncio
async def test_sql_failure_is_unknown_not_present_or_certified():
    result=await observe_runtime_catalog(FakeAcquire(row(),fault=True))
    assert not result.catalog_observed
    assert result.final_accounting_table_present is None
    assert result.exit_cursor_table_present is None
    assert not result.execution_ready_certified
    assert "SECRET" not in result.status


@pytest.mark.asyncio
async def test_missing_or_untyped_metadata_remains_unknown():
    missing=await observe_runtime_catalog(FakeAcquire(None))
    untyped=await observe_runtime_catalog(FakeAcquire(row(fill_sets="true")))
    assert missing.catalog_observed is False
    assert untyped.catalog_observed is False
    assert untyped.fill_sets_table_present is None
