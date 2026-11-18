"""No-credential unit checks for operator-only V40 042–044 DDL plan."""
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
        await m.run_once(apply=False,env=paper_env())


def test_migration_uses_no_backup_artifact_or_external_backup_binary():
    import inspect
    source=inspect.getsource(m)
    assert "backup_file" not in source
    assert "--backup-file" not in source
    assert "pg_restore" not in source
    assert "subprocess" not in source


@pytest.mark.asyncio
async def test_apply_with_unreachable_dsn_does_not_raise_backup_gate():
    env={**paper_env(),"T100_V40_SCHEMA_OWNER_DSN":"postgresql://unused"}
    with pytest.raises(m.V40OwnerMigrationHalted) as exc:
        await m.run_once(apply=True,env=env)
    assert "BACKUP" not in str(exc.value)


def test_no_background_or_render_startup_migration_path():
    import ast
    p=m.ROOT/"app/t100_v40_schema_owner_migration_cli.py"
    tree=ast.parse(p.read_text())
    assert any(isinstance(n,ast.If) for n in tree.body)
    assert "def main()" in p.read_text()
    assert "pg_advisory_xact_lock(10040,31)" in p.read_text()
