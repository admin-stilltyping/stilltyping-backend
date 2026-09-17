import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_catalog_migration_preserves_businesses_and_enforces_keys():
    path = Path(__file__).parents[1] / "migrations/versions/010_products_custom_fields.py"
    spec = importlib.util.spec_from_file_location("catalog_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            connection.execute(text("CREATE TABLE businesses (id CHAR(32) PRIMARY KEY, name TEXT)"))
            connection.execute(
                text("INSERT INTO businesses VALUES ('business', 'Existing business')")
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert {"products", "custom_field_definitions"}.issubset(
                    inspect(connection).get_table_names()
                )
                insert = text("""INSERT INTO products
                    (id,business_id,name,sku,price,currency,status,attributes)
                    VALUES (:id,:business,'Product','SKU',1.50,'INR','active','{}')""")
                connection.execute(insert, {"id": "first", "business": "business"})
                for row in [
                    {"id": "duplicate", "business": "business"},
                    {"id": "orphan", "business": "missing"},
                ]:
                    with pytest.raises(IntegrityError):
                        connection.execute(insert, row)
                migration.downgrade()
                assert (
                    connection.execute(text("SELECT name FROM businesses")).scalar_one()
                    == "Existing business"
                )
                migration.upgrade()
    finally:
        engine.dispose()
