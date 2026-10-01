"""Tests for the connections migration (ga1b2c3d4e5f).

The single-head test is the guard: a stacked PR whose migration chains off a
revision that isn't a real ancestor leaves the tree with two heads, and
``alembic upgrade head`` — which runs on every server boot and every DB-touching
test — then raises. Asserting a single head catches that before CI does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache


def _upgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def _downgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, revision)


def test_single_alembic_head() -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, got {heads!r}"


@pytest.mark.parametrize(
    "previous_head",
    ["ga1b2c3d4e5f", "zb2c3d4e5f6a", "zc3d4e5f6a7b", "ge1b2c3d4e5f", "zd4e5f6a7b8c"],
)
def test_combined_upgrade_preserves_scheduled_tasks(tmp_path: Path, previous_head: str) -> None:
    uri = f"sqlite:///{tmp_path / 'upgrade.db'}"
    engine = sa.create_engine(uri)
    try:
        _upgrade(uri, engine, previous_head)
        with engine.begin() as connection:
            table = sa.Table("scheduled_tasks", sa.MetaData(), autoload_with=connection)
            connection.execute(
                table.insert().values(
                    workspace_id=0,
                    id=bytes.fromhex("00000000000000000000000000000001"),
                    name="Keep task",
                    prompt=b"test",
                    rrule="FREQ=DAILY",
                    agent_id=bytes.fromhex("00000000000000000000000000000002"),
                    created_at=1,
                    updated_at=1,
                )
            )
        _upgrade(uri, engine, "head")
        with engine.connect() as connection:
            table = sa.Table("scheduled_tasks", sa.MetaData(), autoload_with=connection)
            assert connection.execute(sa.select(table.c.name)).scalar_one() == "Keep task"
            assert "requires_hook_review" in table.c
            assert "account_generation" in table.c
            assert "ix_agents_kind_owner_created" in {
                index["name"] for index in sa.inspect(connection).get_indexes("agents")
            }
            assert "connections" in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()


def test_upgrade_creates_table_downgrade_drops_it(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'connections.db'}"
    engine = sa.create_engine(uri)

    # Upgrading to head exercises the full chain onto our migration — this is
    # exactly the call that raises on multiple heads.
    _upgrade(uri, engine, "head")
    inspector = sa.inspect(engine)
    assert "connections" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("connections")}
    assert {
        "workspace_id",
        "user_id",
        "provider",
        "account_id",
        "secret_enc",
        "metadata_json",
        "created_at",
        "updated_at",
    } <= columns
    pk = set(inspector.get_pk_constraint("connections")["constrained_columns"])
    assert pk == {"workspace_id", "user_id", "provider", "account_id"}

    _downgrade(uri, engine, "za2b3c4d5e6f")
    assert "connections" not in sa.inspect(engine).get_table_names()

    engine.dispose()
    clear_engine_cache()
