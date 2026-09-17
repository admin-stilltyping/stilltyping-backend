import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_source_migration_preserves_existing_documents():
    path = Path(__file__).parents[1] / "migrations/versions/009_knowledge_source.py"
    spec = importlib.util.spec_from_file_location("knowledge_source_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT NOT NULL)")
            )
            connection.execute(text("INSERT INTO documents VALUES ('legacy', 'Saved title')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                columns = {
                    column["name"]: column
                    for column in inspect(connection).get_columns("documents")
                }
                assert columns["source_text"]["nullable"]
                assert connection.execute(
                    text("SELECT title, source_text FROM documents")
                ).one() == ("Saved title", None)
                migration.downgrade()
                assert (
                    connection.execute(text("SELECT title FROM documents")).scalar_one()
                    == "Saved title"
                )
                migration.upgrade()
    finally:
        engine.dispose()
