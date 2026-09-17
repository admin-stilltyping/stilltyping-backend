import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_public_chat_upgrade_and_downgrade_preserve_businesses():
    path = Path(__file__).parents[1] / "migrations/versions/013_public_chat.py"
    spec = importlib.util.spec_from_file_location("public_chat_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            connection.execute(text("CREATE TABLE businesses (id CHAR(32) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO businesses VALUES ('a')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert {"web_chat_sessions", "web_chat_turns"} <= set(
                    inspect(connection).get_table_names()
                )
                assert {"session_id", "request_id"} in [
                    set(x["column_names"])
                    for x in inspect(connection).get_unique_constraints("web_chat_turns")
                ]
                migration.downgrade()
                assert inspect(connection).get_table_names() == ["businesses"]
                assert connection.execute(text("SELECT count(*) FROM businesses")).scalar_one() == 1
    finally:
        engine.dispose()
