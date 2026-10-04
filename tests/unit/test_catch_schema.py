import pytest

from modules import catch_schema


class _RecordingConnection:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return "CREATE TABLE"


@pytest.mark.asyncio
async def test_create_tables_is_idempotent_statement_sequence():
    conn = _RecordingConnection()
    await catch_schema.create_tables(conn)
    await catch_schema.create_tables(conn)
    assert len(conn.statements) == len(catch_schema.STATEMENTS) * 2
    assert all("IF NOT EXISTS" in statement for statement in catch_schema.STATEMENTS)
    assert any("catch_cooldowns" in statement for statement in catch_schema.STATEMENTS)


def test_phase_one_tables_have_clone_scope_where_required():
    per_scope = {
        "catch_guild_config", "catch_spawns", "catch_owned", "catch_dex",
        "catch_players", "catch_inventory", "catch_cooldowns", "catch_feature_flags",
        "catch_reminders", "catch_stats_daily",
    }
    for table in per_scope:
        statement = next(s for s in catch_schema.STATEMENTS if f"catch_{table.removeprefix('catch_')}" in s)
        assert "clone_id" in statement, table
        assert "clone_key" in statement, table


def test_audit_and_assets_do_not_store_binary_payloads():
    assets = next(s for s in catch_schema.STATEMENTS if "CREATE TABLE IF NOT EXISTS catch_assets" in s)
    audit = next(s for s in catch_schema.STATEMENTS if "CREATE TABLE IF NOT EXISTS catch_audit" in s)
    assert "vault_message_id" in assets and "content_hash" in assets
    assert "bytea" not in assets.lower()
    assert "detail JSONB" in audit
