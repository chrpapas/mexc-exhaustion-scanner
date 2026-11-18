"""No-credential unit checks for operator-only V40 042–044 DDL plan."""
import asyncio
from pathlib import Path

import pytest

from app import t100_v40_schema_owner_migration_cli as m


def paper_env():
    return dict(
        T100_EXECUTION_MODE="paper",
        T100_LIVE_ORDER_API_ENABLED="false",
        T100_LIVE_KILL_SWITCH="true",
        EXECUTION_ENABLED="false",
        TRADING_MODE="paper",
    )


def test_reviewed_migration_sql_has_exactly_one_removable_outer_transaction():
    for name in m.MIGRATIONS:
        original=(m.ROOT/"migrations"/name).read_text()
        flattened=m.strip_outer_transaction(original)
        assert "BEGIN;" not in flattened.splitlines()
        assert "COMMIT;" not in flattened.splitlines()
        assert "CREATE " in flattened or "ALTER " in flattened
        assert len(flattened)>100


@pytest.mark.parametrize("script",[
    "CREATE TABLE x (y int);",
    "BEGIN;\nCREATE TABLE x(y int);",
    "BEGIN;\nCOMMIT;\nDROP TABLE production;",
    "BEGIN;\n\\i unexpected.sql\nCOMMIT;",
    "BEGIN;\nCOMMIT;\nCOMMIT;",
])
def test_unexpected_sql_wrapper_or_meta_command_cannot_be_executed(script):
    with pytest.raises(m.V40OwnerMigrationHalted):
        m.strip_outer_transaction(script)


@pytest.mark.parametrize("broken",[
    {"T100_EXECUTION_MODE":"live"},
    {"T100_LIVE_ORDER_API_ENABLED":"true"},
    {"T100_LIVE_KILL_SWITCH":"false"},
    {"EXECUTION_ENABLED":"true"},
    {"TRADING_MODE":"live"},
])
def test_local_live_execution_flags_block_schema_mutation(broken):
    assert not m.disabled_local_controls({**paper_env(),**broken})


@pytest.mark.asyncio
async def test_missing_owner_credential_refuses_before_any_network_call():
    with pytest.raises(m.V40OwnerMigrationHalted,match="OWNER_DSN_MISSING"):
        await m.run_once(apply=False,backup_file=None,env=paper_env())


@pytest.mark.asyncio
async def test_missing_backup_refuses_before_any_network_call():
    env={**paper_env(),"T100_V40_SCHEMA_OWNER_DSN":"postgresql://unused"}
    with pytest.raises(m.V40OwnerMigrationHalted,match="BACKUP_FILE_REQUIRED"):
        await m.run_once(apply=True,backup_file=Path("/no/such/file"),env=env)


@pytest.mark.asyncio
async def test_fake_nonempty_backup_is_not_accepted(monkeypatch,tmp_path):
    bad=tmp_path/"not-a-database.dump"
    bad.write_bytes(b"wrong backup"*120)
    env={**paper_env(),"T100_V40_SCHEMA_OWNER_DSN":"postgresql://unused"}
    class InvalidBackup:
        stdout=""
    def fake_restore(*args,**kwargs):
        return InvalidBackup()
    monkeypatch.setattr(m.subprocess,"run",fake_restore)
    with pytest.raises(m.V40OwnerMigrationHalted,
                       match="RESTORABLE_DB_BACKUP_NOT_VERIFIED"):
        await m.run_once(apply=True,backup_file=bad,env=env)


def test_no_background_or_render_startup_migration_path():
    import ast
    p=m.ROOT/"app/t100_v40_schema_owner_migration_cli.py"
    tree=ast.parse(p.read_text())
    assert any(isinstance(n,ast.If) for n in tree.body)
    assert "def main()" in p.read_text()
    assert "pg_advisory_xact_lock(10040,31)" in p.read_text()
